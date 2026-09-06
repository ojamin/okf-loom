"""Behavioral acceptance for the embeddable platform and write transactions."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import multiprocessing
import socket
import threading

import pytest

from okf_loom.client import StudioClient, StudioError
from okf_loom.server import create_server
from okf_loom.studio import Studio, rev_of


@pytest.fixture
def bundle(tmp_path):
    (tmp_path / "topic.md").write_text("---\ntype: Note\ntitle: Topic\n---\n\nOriginal text.\n")
    return tmp_path


def test_deleted_target_is_a_conflict(bundle):
    studio = Studio.for_bundle(bundle)
    path = bundle / "topic.md"
    expected = rev_of(path.read_bytes())
    path.unlink()
    result = studio.save_concept(concept_id="topic", raw="replacement", expected_rev=expected)
    assert result.conflict and result.current_rev is None
    assert not path.exists()


def _process_write(root, expected, ready, go, results, text):
    studio = Studio.for_bundle(root)
    ready.put(True)
    go.wait(10)
    result = studio.save_concept(concept_id="topic", raw=text, expected_rev=expected)
    results.put((result.ok, result.conflict))


def test_process_writers_cannot_both_replace_the_same_revision(bundle):
    ctx = multiprocessing.get_context("spawn")
    ready, results, go = ctx.Queue(), ctx.Queue(), ctx.Event()
    expected = rev_of((bundle / "topic.md").read_bytes())
    workers = [ctx.Process(target=_process_write,
               args=(bundle, expected, ready, go, results, f"writer {i}")) for i in range(2)]
    for worker in workers:
        worker.start()
    try:
        for _ in workers:
            ready.get(timeout=15)
        go.set()
        outcomes = [results.get(timeout=15) for _ in workers]
        assert sorted(outcomes) == [(False, True), (True, False)]
    finally:
        for worker in workers:
            worker.join(timeout=5)
            if worker.is_alive():
                worker.terminate()
                worker.join()


def test_competing_claims_have_one_owner(bundle):
    studio = Studio.for_bundle(bundle)
    comment = studio.post_comment(concept="topic", body="Improve this")
    barrier = threading.Barrier(2)
    def claim(actor):
        other = Studio.for_bundle(bundle)
        barrier.wait(timeout=5)
        try:
            other.update_comment(comment["id"], state="claimed", claimed_by=actor)
            return "claimed"
        except ValueError as exc:
            assert "claim_conflict" in str(exc)
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, ["a", "b"])) == ["claimed", "conflict"]


def test_direct_library_write_cannot_escape_bundle(bundle, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "topic.md"
    result = Studio.for_bundle(bundle).save_concept(concept_id="topic", raw="bad", path=outside)
    assert not result.ok
    assert not outside.exists()


def test_embedding_client_complete_loop_and_shutdown(bundle):
    server = create_server(bundle, port=0)
    port = server.server_address[1]
    with server:
        client = StudioClient(server.url, token=server.csrf_token)
        manifest = client.discover()
        assert manifest["api_version"] == "1"
        assert "replace_text" in manifest["apply_args"]
        assert server.csrf_token not in str(manifest)
        assert str(bundle) not in str(manifest)
        assert client.health()["ok"]
        original = client.document("topic")
        first = client.comment("topic", "Rewrite", idempotency_key="loop-one")["comment"]
        retry = client.comment("topic", "Rewrite", idempotency_key="loop-one")["comment"]
        assert first["id"] == retry["id"]
        assert client.claim(first["id"], actor="host-agent")["comment"]["state"] == "claimed"
        with pytest.raises(StudioError) as conflict:
            client.claim(first["id"], actor="other-agent")
        assert conflict.value.status == 409
        result = client.apply("replace_text", "topic", {"old": "Original", "new": "Revised"},
                              expected_rev=original["rev"], group_id="pass1")
        assert result["applied"]
        assert "Revised" in client.document("topic")["raw"]
        assert client.resolve(first["id"], summary="Revised the topic")["comment"]["state"] == "resolved"
        assert client.undo(group_id="pass1")["ok"]
        assert "Original" in (bundle / "topic.md").read_text()
        for mode in manifest["capabilities"]["search_modes"]:
            assert isinstance(client.search("topic", mode=mode), list)
        with pytest.raises(StudioError) as bad:
            client.request("/__comment", data=[])
        assert bad.value.status == 400
    server.close()  # idempotent
    assert not server._thread.is_alive()
    assert not server.watcher.is_alive()
    assert not server.studio.server_state_path.exists()
    with socket.socket() as sock:
        assert sock.connect_ex(("127.0.0.1", port)) != 0


def test_read_only_client_and_network_boundary(bundle):
    with pytest.raises(ValueError, match="allow_network"):
        create_server(bundle, host="0.0.0.0", port=0)
    with create_server(bundle, port=0, studio_edit=False) as server:
        client = StudioClient(server.url, token=server.csrf_token)
        assert client.discover()["capabilities"]["editing"] is False
        with pytest.raises(StudioError) as forbidden:
            client.comment("topic", "Change")
        assert forbidden.value.status == 403


def test_embedded_servers_isolate_active_code_consent(bundle):
    from urllib.request import urlopen
    (bundle / 'okf-loom.config.yaml').write_text('viewer:\n  allow_active_code: true\n')
    static = bundle / '.okf-loom/viewer/static'
    static.mkdir(parents=True)
    (static / 'wiki.css').write_text('/* CUSTOM ACTIVE ASSET */')
    with create_server(bundle, port=0, watch=False, allow_active_code=True) as allowed:
        with create_server(bundle, port=0, watch=False, allow_active_code=False) as denied:
            with urlopen(allowed.url + '/__static/wiki.css') as response:
                assert b'CUSTOM ACTIVE ASSET' in response.read()
            with urlopen(denied.url + '/__static/wiki.css') as response:
                assert b'CUSTOM ACTIVE ASSET' not in response.read()
            # Reverse request order catches cross-server override-cache leaks.
            with urlopen(allowed.url + '/__static/wiki.css') as response:
                assert b'CUSTOM ACTIVE ASSET' in response.read()


def test_external_changes_are_loaded_before_notification(bundle):
    import queue
    import time
    with create_server(bundle, port=0) as server:
        client = StudioClient(server.url)
        events = server.studio.bus.subscribe()
        try:
            original = (bundle / 'topic.md').read_text()
            other = Studio.for_bundle(bundle)
            result = other.save_concept(concept_id='topic', raw=original.replace('Original', 'External'))
            assert result.ok
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    event = events.get(timeout=0.5)
                except queue.Empty:
                    continue
                if event.get('type') == 'changed':
                    assert 'External' in client.document('topic')['raw']
                    break
            else:
                pytest.fail('External change notification was never delivered')
        finally:
            server.studio.bus.unsubscribe(events)
