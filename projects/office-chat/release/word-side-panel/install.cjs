const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const main='index.DJVdngRf.officeui7.word2.reader2.chatfiles1.js', next=main.slice(0,-3)+'.wordpanel1.js';
const read=n=>fs.readFileSync(path.join(__dirname,n),'utf8');
const once=(s,a,b)=>{assert.equal(s.split(a).length,2,a);return s.replace(a,()=>b)};
function patch(s){
 const wordStart='const WordPages = (() => {',wordEnd='function R1SelectedWord(props){';
 let a=s.indexOf(wordStart),b=s.indexOf(wordEnd);assert(a>=0&&b>a);s=once(s,s.slice(a,b),read('reader.js'));
 a=s.indexOf('function SelectedWordPreview({ path, version })');b=s.indexOf('return SelectedWordPreview(props)}',a);assert(a>=0&&b>a);s=once(s,s.slice(a,b),read('picker.js')+'\n');
 s=once(s,'b=(l?null:v)??y;return','b=(l?null:v)??y,wordPanel=useWordSidePanel(a??null,b!=null);return');
 s=once(s,'(Aj,{panel:b,children:','(Aj,{panel:wordPanel??b,children:');
 s=once(s,'(R1SelectedWord,{path:i.selected,version:i.version}', '(R1SelectedWord,{path:i.selected,version:i.version,onOpen:e}');
 s=once(s,'function pV({conversationId:e,active:t})','function pV({conversationId:e,active:t,onOpenFile})');
 s=once(s,'WordPages,{key:e.filepath,filename:e.filename,load:', 'WordPages,{key:e.filepath,filename:e.filename,onOpen:onOpenFile,load:');
 s=once(s,'(pV,{conversationId:v,active:k})','(pV,{conversationId:v,active:k,onOpenFile:()=>{B("files")(false);t()}})');
 return s;
}
module.exports={main,next,patch};
if(require.main===module){
 const base='/app/client/dist',d=path.join(base,'assets');const names=new Set(fs.readdirSync(d)),graph=new Map([[main,null]]);const load=n=>fs.readFileSync(path.join(d,n),'utf8');
 for(const n of graph.keys()){const refs=[...new Set((load(n).match(/[\w-][\w.-]*\.js\b/g)||[]).filter(m=>names.has(m)))];graph.set(n,refs);for(const r of refs)if(!graph.has(r))graph.set(r,null);}
 const affected=new Set([main]);let prev=0;while(affected.size!==prev){prev=affected.size;for(const [n,refs] of graph)if(refs.some(r=>affected.has(r)))affected.add(n);}
 const rename=n=>n.slice(0,-3)+'.wordpanel1.js',rewrite=s=>s.replace(/[\w-][\w.-]*\.js\b/g,n=>affected.has(n)?rename(n):n);
 for(const n of affected)fs.writeFileSync(path.join(d,rename(n)),rewrite(n===main?patch(load(n)):load(n)));
 for(const n of fs.readdirSync(base).filter(n=>/\.(?:html|js|json|webmanifest)$/.test(n))){const f=path.join(base,n),s=fs.readFileSync(f,'utf8'),v=rewrite(s);if(n==='index.html')assert(v.includes(next));if(v!==s)fs.writeFileSync(f,v);}
}
