'use strict';
const segments = new Intl.Segmenter(undefined,{granularity:'word'});
function passages(query, rows) {
  const terms = new Set(Array.from(segments.segment(query.toLowerCase())).filter(x=>x.isWordLike).map(x=>x.segment));
  return rows.flatMap(row=>{
    if (!/^https?:\/\//i.test(row.url || '') || /^(?:WEB 应用防火墙|安全验证|Just a moment|Access denied|验证码)/i.test((row.title||'').trim())) return [];
    const {raw_content:raw,...result}=row;
    const source=typeof raw==='string'?raw.slice(0,90000):'';
    let best='',highest=0;
    for (const line of source.split(/\r?\n/)) {
      const clean=line.replace(/\[([^\]]*)\]\(https?:\/\/[^)]*\)/g,'$1').trim();
      if (clean.length<16 || (line.match(/\]\(/g)||[]).length>2 || /人机识别检测|请完成安全验证/.test(clean)) continue;
      const pieces=clean.split(/(?<=[。！？!?])|(?<=\.)\s+(?=[A-Z])/u);
      for(const piece of pieces) {
        const normalized=piece.toLowerCase();
        let score=0;
        for (const term of terms) if(normalized.includes(term)) score+=term.length>1?2:1;
        if (score>highest && piece.length<=500) {highest=score;best=piece.trim();}
      }
    }
    if(best && highest>=2) result.content='来源正文：'+best;
    else result.content='搜索摘要（关键数字需要正文支持）：'+String(row.content||'');
    return [result];
  });
}
module.exports={passages};
