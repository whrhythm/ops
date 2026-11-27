const test = require('node:test');
const assert = require('node:assert/strict');
const {createHmac} = require('node:crypto');
const {account, reservations, payload, server} = require('./server.cjs');
const {parseWeather,localWeather,evidence}=require('./weather.cjs');
const {passages}=require('./passages.cjs');

test('station, date and observation freshness are checked before reporting numbers',()=>{
  const now=Date.parse('2026-11-26T11:00:00+08:00');
  const st={province:'福建省',city:'福州',code:'abc',url:'/publish/forecast/AFJ/fuzhou.html'};
  const ask=parseWeather('weather|current|2026-11-26|福建|福州');
  const real={station:st,publish_time:'2026-11-26 10:10',weather:{temperature:18.7}};
  assert.match(evidence(ask,st,{real},now).content,/18\.7℃/);
  assert.equal(evidence(ask,st,{real},now+7200000),null);
  assert.equal(evidence(ask,st,{real:{...real,station:{...st,city:'厦门'}}},now),null);
  assert.equal(evidence(ask,st,{real:{...real,weather:{temperature:9999}}},now),null);
  const row={date:'2026-11-27',day:{weather:{info:'多云',temperature:'22'}},night:{weather:{info:'晴',temperature:'12'}}};
  const predict={station:st,publish_time:'2026-11-26 08:00',detail:[row]};
  assert.equal(evidence({...ask,kind:'forecast'},st,{predict},now),null);
  assert.match(evidence({...ask,kind:'forecast',date:row.date},st,{predict},now).content,/多云 22℃/);
  assert.match(evidence({...ask,kind:'forecast',date:row.date},st,{predict:{...predict,publish_time:'2026-11-26 12:00'}},now).content,/预报时次/);
  assert.equal(evidence(ask,st,{real:{...real,publish_time:'2026-11-26 12:00'}},now),null);
  assert.equal(localWeather('福州 天气 今天 气温',now).province,'福建省');
  assert.equal(localWeather('台风天气对福州交通的影响',now),null);
});
test('ordinary searches select a source sentence, preserve decimals and drop challenge pages',()=>{
  const row={title:'公告',url:'https://example.org/notice',content:'首页',raw_content:'联系我们并查看其他新闻。\n本次列车票价调整为每位旅客 18.75 元，自下月生效。'};
  const [r,...extra]=passages('列车 票价',[row,{...row,title:'WEB 应用防火墙'}]);
  assert.match(r.content,/18\.75/); assert.equal(r.url,row.url);assert.equal(extra.length,0);
  assert.equal(r.raw_content,undefined);
});

test('current local facts refresh page contents while dated news retains the publication filter', () => {
  const daily = payload('/search', {query:'local conditions',country:'china',time_range:'day'});
  assert.equal(daily.country,'china');
  assert.equal(daily.max_age_hours,12);
  assert.equal(daily.time_range,undefined);
  const news = payload('/search', {query:'election',country:'china',time_range:'day',topic:'news'});
  assert.equal(news.time_range,'day');
  assert.equal(news.country,undefined);
  assert.equal(news.max_age_hours,undefined);
});

test('daily limits isolate accounts, roll back rejected reservations and reset at day change', () => {
  const take = reservations(':memory:', 2, 3);
  assert(take('2026-11-25','a','search',1));
  assert(take('2026-11-25','a','search',1));
  assert(!take('2026-11-25','a','search',1));
  assert(take('2026-11-25','b','search',1));
  assert(!take('2026-11-25','b','search',1));
  assert(take('2026-11-25','a','extract',5));
  assert(take('2026-11-26','a','search',1));
});

test('50 searches per account, independent weather and reading budgets, and a shared ceiling',()=>{
  const take=reservations(':memory:',50,100,{weatherHourly:10,extractDaily:50});
  const day='2026-11-26';
  for(const who of ['a','b']) {
    for(let i=0;i<50;i++) assert.equal(take(day,who,'search',1),true);
    assert.equal(take(day,who,'search',1),false);
  }
  assert.equal(take(day,'c','search',1),false);
  assert(take(day,'a','weather',10,0)); assert(!take(day,'a','weather',1,0));
  assert(take(day,'a','extract',50)); assert(!take(day,'a','extract',1));
  assert(take('2026-11-27','a','search',1));
});

test('public weather limits each account per hour while preserving the daily global ceiling',()=>{
  const take=reservations(':memory:',1,2);
  const day='2026-11-26';
  assert(take(day,'a','weather',1,0));
  assert(!take(day,'a','weather',1,1000));
  assert(take(day,'a','weather',1,3600000));
  assert(!take(day,'b','weather',1,3600000));
  assert(take(day,'a','search',1,3600000));
});

test('only signed requests reach the fixed supplier, and exhausted requests never reach it', async t => {
  const secret = 'test credential';
  const now = Date.now();
  const message = 'b'.repeat(64) + '.' + Math.floor(now/1000);
  const token = 'Bearer ' + message + '.' + createHmac('sha256',secret).update('ticket:'+message).digest('hex');
  assert.equal(account(token, secret, now + 7200000),null);
  assert.equal(account(token, 'different', now),null);
  assert.equal(payload('/extract',{urls:['file:///test']}),null);
  assert.equal(payload('/custom',{query:'example'}),null);
  let sent=0;
  const app=server({secret, providerKey:'sample', now:()=>now, reserve:reservations(':memory:',1,2),
    forward:async(url,options)=>{
      sent++;
      assert.equal(url,'https://api.tavily.com/search');
      assert.equal(options.headers.Authorization,'Bearer sample');
      assert.equal(JSON.parse(options.body).search_depth,'basic');
      return Response.json({results:[{title:'Example'}]});
    }});
  await new Promise(resolve=>app.listen(0,'127.0.0.1',resolve));
  t.after(()=>new Promise(resolve=>app.close(resolve)));
  const send=authorization=>fetch('http://127.0.0.1:'+app.address().port+'/search',{
    method:'POST',headers:{authorization},body:JSON.stringify({query:'example',search_depth:'advanced'})
  });
  assert.equal((await send('invalid')).status,403);
  assert.equal((await send(token)).status,200);
  assert.equal((await send(token)).status,429);
  assert.equal(sent,1);
});
