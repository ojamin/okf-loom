// Browser contract harness only. This is explicitly not the Meridian host.
export async function createPluginClient() {
  let disposed=false;
  const entries=new Map();
  return {
    context:{installationId:'test-installation',userId:'test-user',boardId:'test-board',viewId:'test-view',extensionKey:'workspace',extensionType:'board_view',theme:'light'},
    can:()=>!disposed,hasPermission:()=>!disposed,
    net:{fetch:request=>{if(disposed)throw new Error('Disposed');return window.loomTestFetch(request);}},
    settings:{get:async()=>({startConcept:'tables/orders'})},
    storage:{get:async key=>entries.get(key)||null,set:async(key,value)=>{const e={value,version:'v1'};entries.set(key,e);return e;}},
    dispose:()=>{disposed=true;window.loomTestDisposed=true;},
  };
}
