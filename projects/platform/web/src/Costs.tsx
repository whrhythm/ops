import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { api } from './api';
import './costs.css';

type UnitPrices = {input_per_million:string;output_per_million:string;cached_per_million:string|null;cache_write_per_million:string|null};
type Band = UnitPrices & {max_input_tokens:number|null};
type TimeBand = UnitPrices & {starts_at:string};
type PricingRule = {mode:'fixed';fixed:UnitPrices}|{mode:'context';bands:Band[]}|{mode:'time';timezone:string;time_bands:TimeBand[]};
type Quote = {amount:string|null;currency:string;band:{number:number;min_input_tokens:number;max_input_tokens:number|null}|null;time_band?:{starts_at:string;ends_at:string;timezone:string;local_time:string}|null;missing:string[];assumptions?:string[];items:{field:TokenField;tokens:number|null;per_million:string|null;amount:string|null}[]};
type Rate = {id:string;channel:string;model:string;currency:string;input_per_million:string;output_per_million:string;cached_per_million:string|null;effective_at:string;created_by:string;note:string;test_only:boolean;pricing_rule?:PricingRule|null};
type TokenField = 'input_tokens'|'uncached_input_tokens'|'cache_read_tokens'|'cache_write_tokens'|'output_tokens'|'reasoning_tokens'|'total_tokens';
type Receipt = {id:string;owner_id:string;channel:string;model:string;occurred_at:string;call_id:string;response_id:string;ordinal:number;usage:Record<string,unknown>;tokens?:Partial<Record<TokenField,number|null>>;usage_format?:string;usage_state:string;status:string;native_spend:string|null;cost:string|null;currency:string|null;rate_id:string|null;test_price:boolean|null;usage_sent_at:string|null;cost_sent_at:string|null;last_error:string|null;pricing_details?:Omit<Quote,'currency'>|null};
type NativeAudit = {checked_at:string;status:string;stale:boolean;matched?:number;issue_count?:number;issues?:{request_id:string|null;reason:string;receipt_ids:string[]}[]};
type Report = {mode:string;test_mode:boolean;as_of:string;page:number;page_size:number;totals:{receipts:number;prompt_tokens:number;completion_tokens:number;pending_price:number;pending_usage:number;pending_sync:number;native_spend_usd:string;native_unmatched:number;costs:Record<string,string>;test_costs:Record<string,string>;known_usage?:number;known_usage_sent?:number;classified_input?:number};series:{date:string;tokens:number;calls:number}[];items:Receipt[];issues:{id:string;model:string;channel:string;reasons:string[]}[];issue_count:number;sync:{checked_at:string;status:string;differences:{metric:string;channel:string;model:string;currency:string;expected:string;actual:string}[]}|null;native_audit?:NativeAudit|null};
const auditStatus:Record<string,string> = {matched:'原账核对一致', differences:'发现原账差异', empty:'核验范围内暂无账单', unavailable:'无法读取原账', overflow:'原账核验超出本批容量'};
const auditReasons:Record<string,string> = {unbilled_usage_pending:'失败请求未扣费，实际用量未报告，待核对',duplicate_receipt:'回执重复',missing_native:'原账缺少回执',missing_shadow:'原账有记录，影子计量缺少回执',call_mismatch:'调用编号不一致',unconfirmed_usage:'用量待确认',token_mismatch:'用量不一致',spend_mismatch:'原账金额变化'};
const tabs = {overview:'费用总览',details:'消耗明细',rates:'采购价配置',issues:'对账异常'};
const at = (v:string) => new Date(v).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
const amounts = (v:Record<string,string>) => Object.entries(v).map(([c,n])=>`${c} ${n}`).join(' / ') || '暂无已核算记录';
const tokenLabels: Record<TokenField,string> = {input_tokens:'总输入',uncached_input_tokens:'普通输入',
  cache_read_tokens:'缓存读取',cache_write_tokens:'缓存写入',output_tokens:'总输出',
  reasoning_tokens:'推理细项',total_tokens:'合计'};
function tokenValue(receipt:Receipt, field:TokenField) {
  if (receipt.tokens) return receipt.tokens[field] ?? '未报告';
  const legacyField = field === 'input_tokens' ? 'prompt_tokens' : field === 'output_tokens' ? 'completion_tokens' : field;
  const value = receipt.usage[legacyField];
  return typeof value === 'number' ? value : '未报告';
}
const rateFields = {input_per_million:'普通输入',output_per_million:'输出',cached_per_million:'缓存读取',cache_write_per_million:'缓存写入'};
function priceFields(prefix:string) {
  return Object.entries(rateFields).map(([key,label])=><label key={key}>{label} / 百万 tokens
    <input name={prefix+key} required={key==='input_per_million'||key==='output_per_million'} type="number" min="0" max="100000" step="0.000001"/>
  </label>);
}
function readPrices(form:FormData, prefix:string): UnitPrices {
  const value=(field:string)=>String(form.get(prefix+field)??'');
  return {input_per_million:value('input_per_million'),output_per_million:value('output_per_million'),
    cached_per_million:value('cached_per_million')||null,cache_write_per_million:value('cache_write_per_million')||null};
}
function timeRange(startsAt:string, endsAt:string) {
  return `${startsAt}–${endsAt < startsAt ? '次日 ' : ''}${endsAt}`;
}
function savedTimeBands(bands:TimeBand[]) {
  return bands.map((band,i)=><li key={band.starts_at}>
    {timeRange(band.starts_at,bands[(i+1)%bands.length].starts_at)} · 普通输入 {band.input_per_million} · 输出 {band.output_per_million} · 缓存读 {band.cached_per_million??'未填'} · 缓存写 {band.cache_write_per_million??'未填'}
  </li>);
}
function QuoteDetails({quote,currency}:{quote:Omit<Quote,'currency'>;currency:string}) {
  return <div><strong>{quote.amount===null?'待核算':`${currency} ${quote.amount}`}</strong>
    {quote.assumptions?.map(item=><p className="notice" key={item}>估算假设：{item}</p>)}
    {quote.band && <p>第 {quote.band.number} 档 · 总输入 {quote.band.min_input_tokens}–{quote.band.max_input_tokens??'不限'}</p>}
    {quote.time_band && <p>时段 {timeRange(quote.time_band.starts_at,quote.time_band.ends_at)} · {quote.time_band.timezone}
      <br/>调用当地时间 {quote.time_band.local_time.replace('T',' ')}</p>}
    <ul>{quote.items.map(item=><li key={item.field}>{tokenLabels[item.field]}：{item.tokens??'未报告'} × {item.per_million??'未配置'} / 1,000,000 = {item.amount??'待核算'}</li>)}</ul>
    {quote.missing.length>0 && <p>待核对：{quote.missing.map(key=>`${tokenLabels[key.replace(/_price$/,'') as TokenField]??key}${key.endsWith('_price')?'价格':'用量'}`).join('、')}</p>}
  </div>;
}
export default function Costs({onDirty}:{onDirty:(v:boolean)=>void}) {
  const [tab,setTab]=useState<keyof typeof tabs>('overview');
  const [report,setReport]=useState<Report>();
  const [rates,setRates]=useState<Rate[]>([]);
  const [days,setDays]=useState('30'); const [channel,setChannel]=useState('');const [model,setModel]=useState('');
  const [filter,setFilter]=useState('days=30');const [page,setPage]=useState(1);const [revision,setRevision]=useState(0);
  const [busy,setBusy]=useState(false);const [error,setError]=useState('');const [notice,setNotice]=useState('');
  const [dirty,setDirty]=useState(false);const [saveId,setSaveId]=useState(crypto.randomUUID());
  const [mode,setMode]=useState<PricingRule['mode']>('fixed');
  const [bandCount,setBandCount]=useState(2);
  const [timeStarts,setTimeStarts]=useState<string[]>([]);
  const [quoteId,setQuoteId]=useState(''); const [quoteResult,setQuoteResult]=useState<Quote>();
  const [currency,setCurrency]=useState('USD');const [loaded,setLoaded]=useState(false);
  const sortedStarts=[...new Set(timeStarts.filter(Boolean))].sort();
  function timeBandPreview(index:number) {
    if (sortedStarts.length!==bandCount) return null;
    const start=timeStarts[index];
    const next=sortedStarts[(sortedStarts.indexOf(start)+1)%bandCount];
    return <small>{timeRange(start,next)}</small>;
  }
  useEffect(()=>{let active=true;setLoaded(false);setError('');
    Promise.all([api<Report>(`/v1/admin/costs/report?${filter}&page=${page}`),api<{items:Rate[]}>('/v1/admin/costs/rates')])
      .then(([r,p])=>{if(active){setReport(r);setRates(p.items);setLoaded(true);}})
      .catch(e=>{if(active)setError(e.message);});
    return()=>{active=false;};
  },[filter,page,revision]);
  const change=(value:boolean)=>{setDirty(value);onDirty(value);};
  function readRule(form:FormData):PricingRule {
    if(mode==='fixed')return {mode,fixed:readPrices(form,'fixed-')};
    if(mode==='time')return {mode,timezone:String(form.get('timezone')??'').trim(),
      time_bands:Array.from({length:bandCount},(_,i)=>({...readPrices(form,`time-${i}-`),
        starts_at:String(form.get(`time-${i}-start`)??'')})).sort((a,b)=>a.starts_at.localeCompare(b.starts_at))};
    return {mode,bands:Array.from({length:bandCount},(_,i)=>({...readPrices(form,`band-${i}-`),
      max_input_tokens:i===bandCount-1?null:Number(form.get(`band-${i}-upper`))}))};
  }
  async function tryPrice(form:HTMLFormElement){
    const f=new FormData(form);
    setBusy(true);setError('');setQuoteResult(undefined);
    try{setQuoteResult(await api<Quote>('/v1/admin/costs/rates/quote','POST',{
      receipt_id:quoteId,channel:f.get('channel'),model:f.get('model'),currency,pricing_rule:readRule(f),
    }));}catch(e){setError((e as Error).message);}finally{setBusy(false);}
  }
  async function sync(){setBusy(true);setError('');setNotice('');try{
    const r=await api<{status:string}>('/v1/admin/costs/sync','POST',{});
    setNotice(r.status==='matched'?'用量和成本聚合已核对一致。':r.status==='unavailable'?'计量服务暂不可用，原始记录已保留，可恢复后重试。':'已提交同步，聚合可能稍有延迟，请刷新核对。');
    setRevision(v=>v+1);
  }catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  async function save(e:FormEvent<HTMLFormElement>){e.preventDefault();const form=e.currentTarget;const f=new FormData(form);setBusy(true);setError('');
    const pricing_rule=readRule(f);
    const first=pricing_rule.mode==='fixed'?pricing_rule.fixed:pricing_rule.mode==='time'?pricing_rule.time_bands[0]:pricing_rule.bands[0];
    try{await api('/v1/admin/costs/rates','POST',{id:saveId,channel:f.get('channel'),model:f.get('model'),currency,
      input_per_million:first.input_per_million,output_per_million:first.output_per_million,cached_per_million:first.cached_per_million,pricing_rule,
      effective_at:new Date(String(f.get('effective'))+'+08:00').toISOString(),note:f.get('note'),test_only:f.get('test')==='on'});
      change(false);setSaveId(crypto.randomUUID());form.reset();setTimeStarts([]);setQuoteResult(undefined);setQuoteId('');
      setNotice('采购价版本已保存。点击“同步并核对”为未计价记录匹配适用费率；已计价历史保持不变。');setRevision(v=>v+1);
    }catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  return <section className="cost-console" aria-label="用量与费用">
    <div className="notice">原扣费以 LiteLLM 原账为准。采购成本按供应商规则独立核算，测试费率单独展示。</div>
    <div className="cost-tabs" role="tablist" aria-label="费用页面">{Object.entries(tabs).map(([key,label])=><button key={key} role="tab" aria-selected={tab===key} onClick={()=>{
      if(dirty&&key!==tab&&!window.confirm('放弃尚未保存的采购价？'))return;
      if(key!==tab){change(false);setTimeStarts([]);setSaveId(crypto.randomUUID());}setTab(key as keyof typeof tabs);
    }}>{label}</button>)}</div>
    {error&&<p role="alert" className="error">{error}</p>}{notice&&<p role="status" className="notice">{notice}</p>}
    <div className="cost-toolbar"><form onSubmit={e=>{e.preventDefault();setPage(1);setFilter(new URLSearchParams({days,channel,model}).toString());}}>
      <label>时间范围<select value={days} onChange={e=>setDays(e.target.value)}><option value="7">最近7天</option><option value="30">最近30天</option><option value="90">最近90天</option><option value="366">最近一年</option></select></label>
      <label>供应商渠道<input value={channel} onChange={e=>setChannel(e.target.value)} placeholder="全部渠道"/></label>
      <label>实际模型<input value={model} onChange={e=>setModel(e.target.value)} placeholder="全部模型"/></label>
      <button disabled={busy}>筛选</button></form><button className="subtle" disabled={busy} onClick={()=>setRevision(v=>v+1)}>刷新</button>
      <button disabled={busy} onClick={()=>void sync()}>{busy?'处理中…':'同步并核对'}</button></div>
    {!loaded?<p role="status">{error?'未能读取最新费用，请刷新重试。':'正在读取费用…'}</p>:report&&<>
      <p>数据读取于 {at(report.as_of)} · 北京时间。聚合核对范围为全部影子记录。</p>
      {tab==='overview'&&<><div className="cost-stats">
        <article><span>原扣费合计</span><strong>USD {report.totals.native_spend_usd}</strong><small>仅已匹配回执，待匹配 {report.totals.native_unmatched} 笔</small></article>
        <article><span>供应商成本</span><strong>{amounts(report.totals.costs)}</strong><small>测试试算：{amounts(report.totals.test_costs)}</small></article>
        <article><span>实际用量</span><strong>{report.totals.prompt_tokens+report.totals.completion_tokens} tokens</strong><small>输入 {report.totals.prompt_tokens} / 输出 {report.totals.completion_tokens}</small></article>
        <article><span>需要处理</span><strong>{report.issue_count} 笔</strong><small>缺价 {report.totals.pending_price} · 待核用量 {report.totals.pending_usage} · 待同步 {report.totals.pending_sync}</small></article>
      </div><section className="panel"><h2>用量趋势</h2>{report.series.length===0?<p>此范围暂无用量。</p>:report.series.map(d=><div className="cost-bar" key={d.date}><span>{d.date}</span><meter aria-label={d.date+'用量'} value={d.tokens} max={Math.max(...report.series.map(x=>x.tokens),1)}/><span>{d.tokens} tokens · {d.calls} 次</span></div>)}</section>
      <section className="panel"><h2>需要处理</h2><p>未确认采购价格的调用保持待核算；免费消耗、充值及余额尚未接入本页，不以零元代替。</p><button className="subtle" onClick={()=>setTab('issues')}>查看对账与同步状态 →</button></section></>}
      {tab==='details'&&<section className="panel"><h2>消耗明细 · {report.totals.receipts} 笔</h2>
        {report.totals.known_usage !== undefined && <p>已确认输入和输出 {report.totals.known_usage} 笔 · 输入分类齐全 {report.totals.classified_input} 笔 · 已接受用量投递 {report.totals.known_usage_sent} / {report.totals.known_usage} 笔</p>}
        <div className="cost-scroll"><table><thead><tr><th>时间 / 账号</th><th>模型 / 渠道</th><th>总输入</th><th>普通输入</th><th>缓存读取</th><th>缓存写入</th><th>总输出</th><th>原扣费 USD</th><th>采购成本</th><th>详情</th></tr></thead><tbody>{report.items.map(r=><tr key={r.id}>
        <td>{at(r.occurred_at)}<small>{r.owner_id}</small></td><td>{r.model}<small>{r.channel}</small></td>
        {(['input_tokens','uncached_input_tokens','cache_read_tokens','cache_write_tokens','output_tokens'] as const).map(field=><td key={field}>{tokenValue(r,field)}</td>)}
        <td>{r.native_spend??'待核验'}</td><td>{r.cost===null?'待核算':`${r.currency} ${r.cost}`}<small>{r.test_price?'测试试算':''}</small></td>
        <td><details><summary>查看记录</summary><p>调用：{r.call_id}<br/>尝试：{r.ordinal}<br/>供应商响应：{r.response_id||'未取得'}<br/>事件：{r.id}<br/>费率版本：{r.rate_id||'未匹配'}<br/>用量投递：{r.usage_sent_at?'已接受':'待同步'}<br/>成本投递：{r.cost_sent_at?'已接受':'待同步或未计价'}</p>
          <dl>{(Object.keys(tokenLabels) as TokenField[]).map(field=><div key={field}><dt>{tokenLabels[field]}</dt><dd>{tokenValue(r,field)}</dd></div>)}</dl>
          <details><summary>原始用量字段</summary><p>{r.usage_format??'历史回执'}</p><pre>{JSON.stringify(r.usage,null,2)}</pre></details>
          {r.pricing_details && <QuoteDetails quote={r.pricing_details} currency={r.currency??''}/>}
        </details></td>
      </tr>)}</tbody></table></div>{!report.items.length&&<p>当前范围暂无记录。</p>}<div className="cost-pagination"><button disabled={page===1} onClick={()=>setPage(p=>p-1)}>上一页</button><span>第 {page} 页</span><button disabled={page*report.page_size>=report.totals.receipts} onClick={()=>setPage(p=>p+1)}>下一页</button></div></section>}
      {tab==='rates'&&<><section className="panel"><h2>新增采购价版本</h2><p>每百万 token 的价格。生效时间使用北京时间；历史已计价记录不会被覆盖。</p>
        <form className="cost-rate-form" onSubmit={save} onChange={()=>{change(true);setQuoteResult(undefined);}}>
          <label>供应商渠道<input name="channel" required maxLength={128} placeholder="siliconrouter"/></label><label>实际模型<input name="model" required maxLength={200} placeholder="供应商返回的模型名称"/></label>
          <label>币种<select value={currency} onChange={e=>setCurrency(e.target.value)}><option>USD</option><option>CNY</option></select></label>
          <label>计价方式<select value={mode} onChange={e=>{setMode(e.target.value as PricingRule['mode']);setBandCount(2);setTimeStarts([]);}}>
            <option value="fixed">固定单价</option><option value="context">按单次总输入分档</option><option value="time">按每日时段</option>
          </select></label>
          {mode==='time'&&<><label>供应商结算时区<input name="timezone" defaultValue="Asia/Shanghai" required maxLength={64}/></label>
            <p className="cost-wide">填写每段的开始时间。结束时间由下一段决定，最后一段延续到次日第一段。按调用开始时刻匹配。</p></>}
          {mode==='fixed'?priceFields('fixed-'):Array.from({length:bandCount},(_,i)=><fieldset className="cost-wide" key={`${mode}-${i}`}>
            <legend>{mode==='time'?`时段 ${i+1}`:`第 ${i+1} 档`}</legend>
            {mode==='time'
              ? <label>开始时间<input name={`time-${i}-start`} required type="time" value={timeStarts[i]??''}
                  onChange={e=>{const value=e.target.value;setTimeStarts(current=>{
                    const updated=[...current];updated[i]=value;return updated;
                  });}}/>{timeBandPreview(i)}</label>
              : i<bandCount-1?<label>总输入上限（含）<input name={`band-${i}-upper`} required type="number" min="1" max="1000000000" step="1"/></label>:<p>总输入超过上一档上限</p>}
            {priceFields(`${mode==='time'?'time':'band'}-${i}-`)}
          </fieldset>)}
          {mode!=='fixed'&&<div className="cost-wide"><button type="button" disabled={bandCount>=30} onClick={()=>{setBandCount(n=>n+1);change(true);setQuoteResult(undefined);}}>增加{mode==='time'?'时段':'档位'}</button>{' '}
            <button type="button" disabled={bandCount<=2} onClick={()=>{setBandCount(n=>n-1);setTimeStarts(current=>current.slice(0,bandCount-1));change(true);setQuoteResult(undefined);}}>减少{mode==='time'?'时段':'档位'}</button></div>}
          <label>生效时间（北京时间）<input name="effective" type="datetime-local" required/></label>
          <label>价格依据 / 备注<input name="note" required minLength={3} maxLength={500} placeholder="填写供应商价目或测试用途"/></label>
          {report.test_mode&&<label className="cost-check"><input name="test" type="checkbox" defaultChecked/>仅测试费率，允许匹配历史测试回执</label>}
          <label>试算用调用回执编号<input value={quoteId} onChange={e=>setQuoteId(e.target.value)} placeholder="从消耗明细复制事件编号"/></label>
          <button type="button" disabled={busy||!quoteId} onClick={e=>{if(e.currentTarget.form)void tryPrice(e.currentTarget.form);}}>试算这笔调用</button>
          {quoteResult&&<div className="cost-wide" role="status"><QuoteDetails quote={quoteResult} currency={quoteResult.currency}/></div>}
          <button disabled={busy||!dirty}>保存采购价版本</button>
        </form></section><section className="panel"><h2>已保存的价格版本</h2>{rates.length===0?<p>尚无采购价。调用仍保留，成本显示待核算。</p>:<div className="cost-scroll"><table><thead><tr><th>渠道 / 模型</th><th>采购规则（每百万 tokens）</th><th>生效时间</th><th>依据 / 创建人</th></tr></thead><tbody>{rates.map(r=><tr key={r.id}><td>{r.channel}<small>{r.model}</small></td><td>
          {r.pricing_rule?.mode==='context'?<details><summary>{r.currency} · {r.pricing_rule.bands.length} 档</summary><ol>{r.pricing_rule.bands.map((band,i)=><li key={i}>上限 {band.max_input_tokens??'不限'} · 普通输入 {band.input_per_million} · 输出 {band.output_per_million} · 缓存读 {band.cached_per_million??'未填'} · 缓存写 {band.cache_write_per_million??'未填'}</li>)}</ol></details>:
          r.pricing_rule?.mode==='time'?<details><summary>{r.currency} · 每日时段 · {r.pricing_rule.timezone}</summary><ol>{savedTimeBands(r.pricing_rule.time_bands)}</ol></details>:
          <>{r.currency} · 固定单价<small>普通输入 {r.input_per_million} · 输出 {r.output_per_million} · 缓存读 {r.cached_per_million??'未填'} · 缓存写 {r.pricing_rule?.fixed.cache_write_per_million??'未填'}</small></>}
          <small>{r.test_only?'测试价':'供应商采购价'}</small></td><td>{at(r.effective_at)}</td><td>{r.note}<small>{r.created_by} · {r.id}</small></td></tr>)}</tbody></table></div>}</section></>}
      {tab==='issues'&&<><section className="panel"><h2>LiteLLM 原账核验</h2>
        {report.native_audit ? <><p>{report.native_audit.stale?'核验已超时，当前状态待确认':auditStatus[report.native_audit.status]} · 测试范围</p>
          <p>最近核验 {at(report.native_audit.checked_at)} · 匹配 {report.native_audit.matched??'—'} 笔 · 异常 {report.native_audit.issue_count??'—'} 笔</p>
          {report.native_audit.issues?.map((issue,index)=><p key={index}>{auditReasons[issue.reason]} · {issue.request_id??issue.receipt_ids.join(', ')}</p>)}</>:<p>尚未进行自动核验。</p>}</section>
      <section className="panel"><h2>计量同步与聚合核对</h2><p>{!report.sync?'尚未进行核对':report.sync.status==='matched'?'上次核对一致':report.sync.status==='unavailable'?'计量服务不可用，不能确认聚合结果':'聚合待追齐或存在差异，请刷新核对'}</p>{report.sync&&<p>最近核对：{at(report.sync.checked_at)}。事件去重窗口为32天，超过31天的未确认投递转人工核验。</p>}{report.sync?.differences.map((d,i)=><p key={i}>{d.channel} / {d.model} / {d.metric}：原始记录 {d.expected}，聚合 {d.actual} {d.currency}</p>)}</section>
      <section className="panel"><h2>待处理记录 · {report.issue_count} 笔</h2><p>最多展示前100笔。配置采购价后同步；缺少用量或原账时需核验供应商回执，不可手动标成零元。</p>{report.issues.map(r=><article className="cost-issue" key={r.id}><strong>{r.model}</strong><p>{r.channel} · {r.id}</p><p>{r.reasons.join('；')}</p>{r.reasons.some(x=>x.includes('采购价'))&&<button className="subtle" onClick={()=>setTab('rates')}>配置采购价 →</button>}</article>)}{report.issue_count===0&&<p>当前筛选范围无待处理记录。</p>}</section></>}
    </>}
  </section>;
}
