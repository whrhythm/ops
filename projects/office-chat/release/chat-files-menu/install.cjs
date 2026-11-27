const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const main = 'index.DJVdngRf.officeui7.word2.reader2.js';
const zh = 'locale-zh-Hans.Bl4uiZpp.officeui7.js';
const files = [main, zh];
const once = (s, old, next) => {
  assert.equal(s.split(old).length, 2, old);
  return s.replace(old, () => next);
};
function patch(name, s) {
  if (name === zh) {
    const dict = JSON.parse(fs.readFileSync(path.join(__dirname,'translation.json'),'utf8'));
    const entries = Object.entries(dict).filter(([key]) => /^com_office_(tasks|task_|output_expired)/.test(key));
    for (const [marker, getter] of [['com_ui_save:()=>kC',true],['com_ui_save:kC',false]]) {
      s = once(s, marker, entries.map(([k,v]) => k+':'+(getter?'()=>':'')+JSON.stringify(v)).join(',')+','+marker);
    }
    return s;
  }
  s = once(s, 'com_office_tasks:`File tasks`', 'com_office_tasks:"Files in this chat",com_office_tasks_empty:"No file tasks in this chat yet.",com_office_task_details:"Execution details"');
  s = once(s, '!t&&(0,$.jsx)(pV,{conversationId:r,active:_.isSubmitting}),', '');
  s = once(s, 'return!n?.officeTasks||!o.isError&&!o.data?.length?null:(0,$.jsxs)(`details`,{className:`border-border-light bg-surface-primary text-text-primary relative mx-3 mt-14 max-h-[35dvh] shrink-0 overflow-y-auto rounded-lg border px-3 py-2 text-sm`,children:[(0,$.jsx)(`summary`,{className:`cursor-pointer`,children:a(`com_office_tasks`)})',
    'return!n?.officeTasks?null:(0,$.jsxs)("div",{className:"text-text-primary min-h-0 overflow-y-auto text-sm",children:[o.isLoading&&(0,$.jsx)("p",{role:"status",children:a("com_ui_loading")}),o.isSuccess&&!o.data.length&&(0,$.jsx)("p",{children:a("com_office_tasks_empty")})');
  const stdout='(0,$.jsx)(`pre`,{className:`max-h-48 overflow-auto break-words whitespace-pre-wrap`,children:n.stdout.slice(0,8e3)})';
  s=once(s,stdout,'(0,$.jsxs)("details",{children:[(0,$.jsx)("summary",{children:a("com_office_task_details")}),'+stdout+']})');
  s=once(s,'le=!O,ue=[...g.items','le=!O,filesEnabled=Mv().data?.officeTasks,filesRef=(0,Q.useRef)(null),ue=[...(filesEnabled&&v&&v!=="new"?[{label:i("com_office_tasks"),icon:(0,$.jsx)(ne,{className:mW,"aria-hidden":true}),onClick:()=>B("files")(true),ariaHasPopup:"dialog",ariaControls:"conversation-files-dialog",hideOnClick:false,ref:filesRef,render:e=>(0,$.jsx)("button",{...e})}]:[]),...g.items');
  return once(s,'children:[g.dialogs,R&&','children:[g.dialogs,filesEnabled&&L("files")&&(0,$.jsx)(Ho,{open:true,onOpenChange:B("files"),triggerRef:filesRef,children:(0,$.jsxs)(Sa,{id:"conversation-files-dialog",className:"flex w-11/12 max-w-lg flex-col",style:{maxHeight:"80dvh"},children:[(0,$.jsx)(es,{children:(0,$.jsx)(as,{children:i("com_office_tasks")})}),(0,$.jsx)(pV,{conversationId:v,active:k})]})}),R&&');
}
module.exports={files,patch};
if(require.main===module){
  const base='/app/client/dist',dir=path.join(base,'assets'),names=new Set(fs.readdirSync(dir)),read=n=>fs.readFileSync(path.join(dir,n),'utf8');
  const graph=new Map([[main,null]]);for(const n of graph.keys()){const refs=[...new Set((read(n).match(/[\w-][\w.-]*\.js\b/g)||[]).filter(n=>names.has(n)))];graph.set(n,refs);for(const r of refs)if(!graph.has(r))graph.set(r,null);}
  const affected=new Set(files);let prior=0;while(affected.size!==prior){prior=affected.size;for(const [n,refs] of graph)if(refs.some(r=>affected.has(r)))affected.add(n);}
  const rename=n=>n.slice(0,-3)+'.chatfiles1.js',refs=s=>s.replace(/[\w-][\w.-]*\.js\b/g,n=>affected.has(n)?rename(n):n);
  for(const n of affected)fs.writeFileSync(path.join(dir,rename(n)),refs(files.includes(n)?patch(n,read(n)):read(n)));
  for(const n of fs.readdirSync(base).filter(n=>/\.(?:html|js|json|webmanifest)$/.test(n))){const f=path.join(base,n),s=fs.readFileSync(f,'utf8'),next=refs(s);if(n==='index.html')assert(next.includes(rename(main)));if(s!==next)fs.writeFileSync(f,next);}
}
