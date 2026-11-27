import { createManagedPurchasesHandler } from './managedPurchases';

it('derives the buyer server-side and never returns the loopback checkout address', async () => {
  const fetch = jest.fn().mockResolvedValue(new Response(JSON.stringify({state:'confirmed',checkout_url:'http://127.0.0.1:9066/checkout/test'})));
  const handler = createManagedPurchasesHandler({url:'https://platform.test/internal/v1/workspace',
    issuer:'https://login.test',serviceToken:'server-token',subject:'buyer',fetch});
  const req = {method:'POST',params:{orderId:'12345678-1234-1234-1234-123456789012',action:'checkout'},
    user:{provider:'openid',openidId:'buyer',openidIssuer:'https://login.test'},
    body:{price:0,subject:'other'},get:()=>undefined} as never;
  const res = {setHeader:jest.fn(),status:jest.fn().mockReturnThis(),json:jest.fn(),sendStatus:jest.fn()} as never;
  await handler(req,res,jest.fn());
  expect(fetch).toHaveBeenCalledWith(new URL('https://platform.test/internal/v1/workspace/capacity/orders/12345678-1234-1234-1234-123456789012/checkout'),
    expect.objectContaining({headers:{'X-CodeChat-Service-Token':'server-token','X-CodeChat-Subject':'buyer'}}));
  expect((res as {json:jest.Mock}).json).toHaveBeenCalledWith({enabled:true,state:'confirmed',checkout_url:null});
});

it('does not expose purchase actions to another account', async () => {
  const fetch=jest.fn();
  const handler=createManagedPurchasesHandler({url:'https://platform.test',issuer:'https://login.test',
    subject:'buyer',serviceToken:'server-token',fetch});
  const res={setHeader:jest.fn(),status:jest.fn().mockReturnThis(),json:jest.fn()} as never;
  await handler({method:'GET',user:{provider:'openid',openidId:'other',openidIssuer:'https://login.test'}} as never,res,jest.fn());
  expect(fetch).not.toHaveBeenCalled();
  expect((res as {json:jest.Mock}).json).toHaveBeenCalledWith({enabled:false});
});

it('admits ordinary verified accounts only when explicitly enabled for all', async () => {
  const fetch=jest.fn().mockResolvedValue(new Response(JSON.stringify({plans:[]})));
  const handler=createManagedPurchasesHandler({url:'https://platform.test',issuer:'https://login.test',
    subject:'*',serviceToken:'server-token',fetch});
  for (const issuer of ['https://wrong.test','https://login.test']) {
    const res={setHeader:jest.fn(),status:jest.fn().mockReturnThis(),json:jest.fn()} as never;
    await handler({method:'GET',params:{},user:{provider:'openid',openidId:'ordinary-user',openidIssuer:issuer}} as never,res,jest.fn());
    expect(fetch).toHaveBeenCalledTimes(issuer==='https://login.test'?1:0);
  }
});
