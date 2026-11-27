'use strict';
const http = require('node:http');
const { createHmac, timingSafeEqual } = require('node:crypto');
const { DatabaseSync } = require('node:sqlite');
const { parseWeather, localWeather, weatherClient } = require('./weather.cjs');
const { passages } = require('./passages.cjs');

function account(token, secret, now) {
  const match = /^Bearer ([a-f0-9]{64})\.(\d{10})\.([a-f0-9]{64})$/.exec(token || '');
  if (!match || Math.abs(Math.floor(now / 1000) - Number(match[2])) > 3600) return null;
  const expected = createHmac('sha256', secret).update('ticket:' + match[1] + '.' + match[2]).digest();
  return timingSafeEqual(expected, Buffer.from(match[3], 'hex')) ? match[1] : null;
}

function reservations(path, userLimit, totalLimit, {weatherHourly = userLimit, extractDaily = userLimit * 5} = {}) {
  const db = new DatabaseSync(path);
  db.exec('CREATE TABLE IF NOT EXISTS daily (day TEXT, subject TEXT, kind TEXT, units INTEGER NOT NULL, PRIMARY KEY(day, subject, kind))');
  const take = db.prepare('INSERT INTO daily VALUES (?, ?, ?, ?) ON CONFLICT(day,subject,kind) DO UPDATE SET units=units+excluded.units WHERE units+excluded.units<=?');
  const prune = db.prepare('DELETE FROM daily WHERE day < ?');
  return (day, subject, kind, units, timestamp) => {
    const multiplier = kind === 'extract' ? 5 : 1;
    db.exec('BEGIN IMMEDIATE');
    try {
      prune.run(day);
      const personalLimit = kind === 'weather' ? weatherHourly : kind === 'extract' ? extractDaily : userLimit;
      for (const [who, limit] of [[subject, personalLimit], ['*', totalLimit * multiplier]]) {
        const bucket=kind==='weather' && who!=='*' ? kind+'-'+Math.floor(timestamp/3600000) : kind;
        if (units > limit || !take.run(day, who, bucket, units, limit).changes) {
          db.exec('ROLLBACK');
          return false;
        }
      }
      db.exec('COMMIT');
      return true;
    } catch (err) {
      db.exec('ROLLBACK');
      throw err;
    }
  };
}

function payload(path, body) {
  if (path === '/search' && typeof body?.query === 'string' && body.query.trim() && body.query.length <= 1200) {
    const topic = body.topic === 'news' ? 'news' : 'general';
    const range = body.time_range;
    const daily = topic === 'general' && (['d','day'].includes(range) ||
      (!range && /今天|今日|实时|\btoday\b|\bright now\b/iu.test(body.query)));
    return { query: body.query, search_depth: 'basic', max_results: daily ? 8 : 4,
      include_answer: false, include_images: false, include_published_date: true, include_raw_content:'text', topic,
      ...(topic === 'general' && typeof body.country === 'string' && /^[a-z ]{2,40}$/.test(body.country) && {country:body.country}),
      ...(daily ? {max_age_hours:12} : ['day','week','month','year','d','w','m','y'].includes(range) && {time_range:range}) };
  }
  if (path === '/extract' && Array.isArray(body?.urls) && body.urls.length > 0 && body.urls.length <= 5 &&
      body.urls.every(url => typeof url === 'string' && url.length <= 4096 && /^https?:\/\//i.test(url))) {
    return { urls: body.urls, extract_depth: 'basic', format: 'markdown', include_images: false };
  }
  return null;
}

function server({secret, providerKey, reserve, forward = fetch, now = Date.now}) {
  const weather = weatherClient(forward);
  const checkedPages = new Map();
  return http.createServer(async (req, res) => {
    const reply = (status, data) => {res.writeHead(status, {'content-type':'application/json'});res.end(JSON.stringify(data));};
    if (req.method === 'GET' && req.url === '/health') return reply(200, {ready:true});
    if (req.method !== 'POST' || !['/search','/extract'].includes(req.url)) return reply(404, {});
    const timestamp = now();
    const subject = account(req.headers.authorization, secret, timestamp);
    if (!subject) return reply(403, {error:'Search authorization required'});
    try {
      const chunks = [];
      let size = 0;
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 32768) return reply(413, {});
        chunks.push(chunk);
      }
      let body;
      try {body = JSON.parse(Buffer.concat(chunks).toString());} catch {return reply(400, {});}
      const data = payload(req.url, body);
      if (!data) return reply(400, {error:'Invalid search request'});
      const day = new Date(timestamp + 8 * 3600000).toISOString().slice(0,10);
      const lookup = req.url==='/search' && (parseWeather(data.query) || localWeather(data.query,timestamp));
      const read=req.url==='/search' && /^read\|(https:\/\/[^\s|]+)\|([^|]{1,300})$/iu.exec(data.query);
      if (read) {
        let url;try{url=new URL(read[1]);}catch{return reply(400,{error:'Invalid article URL'});}
        if (url.username || url.password || url.port || !/^[a-z\d-]+(?:\.[a-z\d-]+)*\.[a-z]{2,}$/i.test(url.hostname) ||
            /\.(local|internal|localhost|test|invalid)$/i.test(url.hostname)) return reply(400,{error:'Public article URL required'});
      }
      const extracts=[];
      if(data.urls) {
        data.urls=data.urls.filter(url=>{
          const page=checkedPages.get(url);
          if(!page || page.until<=timestamp) return true;
          extracts.push({url,raw_content:page.content});
          return false;
        });
        if (!data.urls.length) return reply(200,{results:extracts,failed_results:[]});
      }
      if (!reserve(day, subject, lookup?'weather':read?'extract':req.url.slice(1), data.urls?.length ?? 1, timestamp)) {
        return reply(429, {error:lookup?'天气查询达到账号小时上限或平台当日上限，请稍后再试。':'今日联网搜索或原文阅读次数已用完，请明天再试。'});
      }
      const signal = AbortSignal.timeout(18000);
      if (lookup) {
        const result = await weather(lookup,timestamp,signal);
        if(result) {
          result.url+='#'+lookup.kind+'-'+lookup.date;
          if(checkedPages.size>=512) checkedPages.delete(checkedPages.keys().next().value);
          checkedPages.set(result.url,{until:timestamp+180000,content:result.content});
        }
        return reply(200,{results:result?[result]:[]});
      }
      const request = params => forward('https://api.tavily.com' + (read?'/extract':req.url), {
        method:'POST', redirect:'error', signal,
        headers:{Authorization:'Bearer ' + providerKey,'Content-Type':'application/json'},
        body:JSON.stringify(params),
      });
      let upstream = await request(read?{urls:[read[1]],format:'text',extract_depth:'basic',include_images:false}:data);
      if (upstream.status === 434 && data.max_age_hours != null) {
        const {max_age_hours, ...cached} = data;
        upstream = await request(cached);
      }
      if (!upstream.ok) return reply(upstream.status, {error:'Search provider unavailable'});
      const response=await upstream.json();
      if(read){
        const rows=(response.results||[]).filter(row=>row.raw_content && row.url===read[1]).map(row=>({...row,title:row.title||new URL(row.url).hostname,content:''}));
        const results=passages(read[2],rows).filter(row=>row.content.startsWith('来源正文：'));
        for(const result of results){
          if(checkedPages.size>=512)checkedPages.delete(checkedPages.keys().next().value);
          const full=rows.find(row=>row.url===result.url);
          checkedPages.set(result.url,{until:timestamp+180000,content:full.raw_content.slice(0,16000)});
        }
        return reply(200,{results});
      }
      if(req.url==='/search' && Array.isArray(response.results)) response.results=passages(data.query,response.results);
      if(extracts.length) response.results=[...extracts,...(response.results||[])];
      return reply(200, response);
    } catch {return reply(502, {error:'Search temporarily unavailable'});}
  });
}
if (require.main === module) {
  const { SEARCH_TICKET_SECRET:secret, TAVILY_API_KEY:providerKey } = process.env;
  const limits = [process.env.SEARCH_USER_DAILY, process.env.SEARCH_GLOBAL_DAILY].map(Number);
  const weatherHourly = Number(process.env.WEATHER_USER_HOURLY ?? limits[0]);
  const extractDaily = Number(process.env.EXTRACT_USER_DAILY ?? limits[0] * 5);
  if (!secret || !providerKey || ![...limits, weatherHourly, extractDaily].every(n => Number.isSafeInteger(n) && n > 0)) throw Error('Search configuration incomplete');
  server({secret, providerKey, reserve:reservations('/data/quota.sqlite', ...limits, {weatherHourly, extractDaily})}).listen(8080);
}
module.exports = {account, reservations, payload, server};
