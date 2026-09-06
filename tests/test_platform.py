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


def test_http_undo_publishes_after_document_refresh(bundle, monkeypatch):
    from okf_loom.model import Bundle
    with create_server(bundle, port=0, watch=False) as server:
        client = StudioClient(server.url, token=server.csrf_token)
        events = server.studio.bus.subscribe()
        client.apply('replace_text', 'topic', {'old': 'Original', 'new': 'Revised'}, group_id='undo-order')
        while events.get(timeout=2)['type'] != 'changed':
            pass
        original_load = Bundle.load
        def checked_load(*args, **kwargs):
            # While undo is refreshing the read model, a live client must
            # not yet receive an event directing it to fetch that model.
            assert events.empty(), 'undo notified clients before refreshing content'
            return original_load(*args, **kwargs)
        monkeypatch.setattr(Bundle, 'load', checked_load)
        assert client.undo(group_id='undo-order')['ok']
        assert events.get(timeout=2)['type'] == 'activity'
        assert 'Original' in client.document('topic')['raw']
        server.studio.bus.unsubscribe(events)


def test_replay_uses_unique_event_cursor_for_comment_transitions(bundle):
    studio = Studio.for_bundle(bundle)
    note = studio.post_comment(concept='topic', body='Request')
    studio.update_comment(note['id'], state='claimed', claimed_by='agent')
    cursor = studio.read_events(order='desc', limit=1)[0]['event_id']
    studio.resolve_comment(note['id'], reply='Done')
    rows = studio.read_events(since=cursor)
    assert len(rows) == 1
    assert rows[0]['state'] == 'resolved'
    assert rows[0]['id'] == note['id']
    assert rows[0]['event_id'] != cursor


def test_exact_source_save_preserves_bytes_and_rejects_stale_or_invalid_writes(bundle):
    with create_server(bundle, port=0, watch=False) as server:
        client = StudioClient(server.url, token=server.csrf_token)
        original = client.document('topic')
        assert original['source'] == (bundle / 'topic.md').read_text()
        source = original['source'].replace('title: Topic', 'title: "Revised"\ncustom: {keep: [1, 2]} # deliberate style')
        result = client.request('/__save', data={'id':'topic', 'source':source, 'expected_rev':original['rev']})
        assert result['ok']
        assert (bundle / 'topic.md').read_text() == source
        assert client.document('topic')['source'] == source
        with pytest.raises(StudioError) as conflict:
            client.request('/__save', data={'id':'topic','source':original['source'],'expected_rev':original['rev']})
        assert conflict.value.status == 409
        for invalid, status in [({'source':'no frontmatter','expected_rev':result['rev']},400),
                                ({'source':source},400),
                                ({'source':source,'expected_rev':None},409),
                                ({'source':source+'\n[Broken](/absent.md)\n','expected_rev':result['rev']},422)]:
            with pytest.raises(StudioError) as rejected:
                client.request('/__save', data={'id':'topic',**invalid})
            assert rejected.value.status == status
            assert (bundle / 'topic.md').read_text() == source
        assert client.undo(concept='topic', rev=result['snap_rev'])['ok']
        assert client.document('topic')['source'] == original['source']


def test_source_create_is_exclusive_and_protects_reserved_files(bundle):
    with create_server(bundle, port=0, watch=False) as server:
        client = StudioClient(server.url, token=server.csrf_token)
        source = '---\ntype: Note\nextra: yes\n---\n\n[Later](/later.md)\n'
        for cid in ['index','nested/log','../escape']:
            with pytest.raises(StudioError) as error:
                client.request('/__save', data={'id':cid,'source':source,'expected_rev':None})
            assert error.value.status == 400
        body = {'id':'nested/new','source':source,'expected_rev':None,'allow_forward_reference':True}
        assert client.request('/__save', data=body)['ok']
        assert client.document('nested/new')['source'] == source
        with pytest.raises(StudioError) as error:
            client.request('/__save', data=body)
        assert error.value.status == 409


def test_undo_creation_removes_the_file_and_undoing_that_restores_it(bundle):
    with create_server(bundle, port=0, watch=False) as server:
        client = StudioClient(server.url, token=server.csrf_token)
        source = '---\ntype: Note\n---\nCreated in a host workspace.\n'
        created = client.request('/__save', data={'id':'created','source':source,'expected_rev':None})
        assert created['snap_rev'] == 'absent'
        assert client.undo(concept='created',rev='absent')['ok']
        assert not (bundle/'created.md').exists()
        with pytest.raises(StudioError) as repeated:
            client.undo(concept='created',rev='absent')
        assert repeated.value.status == 409
        assert client.undo(concept='created',rev=created['rev'])['ok']
        assert client.document('created')['source'] == source


def test_source_save_requires_auth_and_obeys_read_only_mode(bundle):
    for read_only in [False, True]:
        with create_server(bundle, port=0, studio_edit=not read_only) as server:
            client = StudioClient(server.url, token=server.csrf_token if read_only else '')
            with pytest.raises(StudioError) as error:
                client.request('/__save', data={'id':'topic','source':'---\ntype: Note\n---\n','expected_rev':None})
            assert error.value.status == 403
