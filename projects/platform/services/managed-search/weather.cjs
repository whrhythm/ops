'use strict';
const origin = 'https://www.nmc.cn';
const locations = require('./weather-locations.json');
const dayKey = ms => new Date(ms + 8 * 3600000).toISOString().slice(0,10);
const place = value => String(value || '').trim().replace(/(?:特别行政区|壮族自治区|维吾尔自治区|回族自治区|自治区|省|市)$/u,'');
const degree = value => (typeof value === 'number' || (typeof value === 'string' && /^-?\d+(?:\.\d+)?$/.test(value))) &&
  Number.isFinite(Number(value)) && Number(value)>=-90 && Number(value)<=65;
function fresh(time, now, ageHours, leadHours=0) {
  if (!/^\d{4}-\d\d-\d\d \d\d:\d\d$/.test(time || '')) return false;
  const delta = now - Date.parse(time.replace(' ','T')+':00+08:00');
  return delta>=-leadHours*3600000 && delta<=ageHours*3600000;
}
function localWeather(query, now) {
  if (!/天气|气温|预报|温度/.test(query)) return null;
  let rest=query.replace(/[“”"'？，?，。]/gu,'').trim();
  let date=dayKey(now);
  const explicit=rest.match(/(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?/);
  if(explicit){
    date=[explicit[1],explicit[2].padStart(2,'0'),explicit[3].padStart(2,'0')].join('-');
    rest=rest.replace(explicit[0],'');
  }else if(/明天|明日|后天/.test(rest))date=dayKey(now+(rest.includes('后天')?2:1)*86400000);
  const kind=/现在|当前|此刻|实时|实况/.test(rest)&&!/最高|最低|预报/.test(rest)?'current':'forecast';
  rest=rest.replace(/天气预报|天气情况|最高|最低|气温|温度|天气|预报|今天|今日|明天|明日|后天|现在|当前|此刻|实时|实况|风力|风向|多少度|多少|怎么样|如何|的|\s+/gu,'');
  const matches=locations.filter(([city,province])=>[place(city),place(province)+place(city),province+city].includes(place(rest)));
  return matches.length===1?{kind,date,city:matches[0][0],province:matches[0][1]}:null;
}
function parseWeather(query) {
  const m = /^weather\|(forecast|current)\|(\d{4}-\d{2}-\d{2})\|([\p{L}· ]{1,25})\|([\p{L}· ]{1,30})$/iu.exec(query.trim());
  return m && {kind:m[1].toLowerCase(),date:m[2],province:m[3].trim(),city:m[4].trim()};
}
function evidence(request, station, data, now) {
  const current = request.kind==='current';
  const item = current ? data?.real : data?.predict;
  if (!item || station.code!==item.station?.code || station.city!==item.station.city ||
      station.province!==item.station.province || !fresh(item.publish_time,now,current?2:36,current?0:3)) return null;
  const label = station.province+' '+station.city;
  let summary;
  if (current) {
    if (request.date!==dayKey(now) || !item.publish_time.startsWith(request.date) || !degree(item.weather?.temperature)) return null;
    summary = label+' 实况，观测时间 '+item.publish_time+'（北京时间），气温 '+item.weather.temperature+'℃。';
  } else {
    const row = item.detail?.find(entry=>entry.date===request.date);
    const day=row?.day?.weather, night=row?.night?.weather;
    if (request.date<dayKey(now) || !day || !night || !degree(day.temperature) || !degree(night.temperature) ||
        ![day.info,night.info].every(value=> typeof value==='string' && /\p{L}/u.test(value))) return null;
    summary = label+' '+row.date+' 预报，白天'+day.info+' '+day.temperature+'℃，夜间'+night.info+' '+night.temperature+
      '℃。来源标注的预报时次 '+item.publish_time+'（北京时间）。';
  }
  if (!/^\/publish\/forecast\/[A-Z]+\/[a-z\d_-]+\.html$/i.test(station.url)) return null;
  return {title:'中央气象台 · '+label+(current?'实况':'天气预报'),url:origin+station.url,content:summary,score:1};
}
function weatherClient(forward=fetch) {
  const cache = new Map();
  async function load(path,ttl,now,signal) {
    const hit=cache.get(path);
    if (hit && hit.until>now) return hit.data;
    const response=await forward(origin+path,{redirect:'error',signal});
    if (!response.ok) throw Error('Weather source unavailable');
    const data=await response.json();
    if(cache.size>=512) cache.delete(cache.keys().next().value);
    cache.set(path,{data,until:now+ttl});
    return data;
  }
  return async (request,now,signal) => {
    const regions=await load('/rest/province',3600000,now,signal);
    const region=regions.find(r=>place(r.name)===place(request.province) && /^[A-Z]{3}$/.test(r.code));
    if (!region) return null;
    const stations=await load('/rest/province/'+region.code,3600000,now,signal);
    const found=stations.filter(s=>place(s.city)===place(request.city) && place(s.province)===place(region.name) && /^[a-z\d]{1,16}$/i.test(s.code));
    if (found.length!==1) return null;
    const station=found[0];
    const report=await load('/rest/weather?stationid='+station.code,180000,now,signal);
    return report.code===0 ? evidence(request,station,report.data,now) : null;
  };
}
module.exports={parseWeather,localWeather,weatherClient,evidence};
