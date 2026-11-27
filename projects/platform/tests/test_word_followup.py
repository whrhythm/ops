import pytest

from control_plane.daily_chat import word_followup


def msg(role, content, **fields):
    return dict(role=role, content=content, **fields)


def request(question='把比赛详情情况做成doc文档'):
    return {'messages': [msg('system','权限约定'), msg('user','旧话题'), msg('assistant','旧内容'),
                         msg('user','介绍这场比赛'), msg('assistant','',tool_calls=[{'id':'search1'}]),
                         msg('tool','Anchor: turn1search2\nURL: https://example.org/a\nSummary: 检索摘录',
                             tool_call_id='search1'), msg('assistant','比赛原有结论 turn1search2'),
                         msg('user',question)],
            'tools':[{'type':'function','function':{'name':n}} for n in ('web_search','execute_code')]}


def test_existing_answer_export_omits_search_and_keeps_source_and_execution_receipt():
    req = request()
    call = msg('assistant','',tool_calls=[{'id':'execute1'}])
    receipt = msg('tool','文件生成记录',tool_call_id='execute1')
    req['messages'].extend([call, receipt])
    result = word_followup(req)
    assert [t['function']['name'] for t in result['tools']] == ['execute_code']
    assert result['messages'][-2:] == [call, receipt]
    assert 'https://example.org/a' in str(result['messages'])
    assert all(x not in str(result['messages']) for x in ['旧话题','检索摘录'])
    assert result['messages'][0] == req['messages'][0]
    assert len(req['tools']) == 2


def test_repeat_after_quota_refusal_uses_last_successful_answer():
    req = request()
    req['messages'] += [msg('assistant','本次未派发 [用量](#starbridge-billing)'), req['messages'][-1]]
    assert '比赛原有结论' in str(word_followup(req)['messages'])


@pytest.mark.parametrize('question',['查今天的比赛并生成word报告','把最新比赛结果做成doc文档',
                                     '把销售统计做成 Word 文档','把比赛情况做成doc文档并补充最新战况'])
def test_other_topics_and_fresh_research_use_normal_workflow(question):
    assert word_followup(request(question)) is None


def test_disabled_tool_or_attached_image_keeps_normal_workflow():
    req = request()
    assert word_followup({**req,'tools':req['tools'][:1]}) is None
    assert word_followup({**req,'tool_choice':'none'}) is None
    req['messages'][-1]['content'] = [{'type':'text','text':'把上述内容做成word文档'},{'type':'image_url'}]
    assert word_followup(req) is None


def test_finish_export_requires_owned_matching_completed_word_receipt(harness):
    import hashlib
    from types import SimpleNamespace
    from datetime import timedelta
    from control_plane.daily_chat import completed_word_export
    from control_plane.models import CodeExecution, OfficeTask, now
    from test_daily_free import owner
    client,_=harness
    req=request()
    req['messages'] += [msg('assistant','',tool_calls=[{'id':'call-1','function':{'name':'execute_code'}}]),
                        msg('tool','saved document.docx',tool_call_id='call-1')]
    with client.session_factory() as db:
        user=owner(db)
        stamp=now()
        assert not completed_word_export(db,user,req,stamp)
        execution=CodeExecution(environment_id=user.id,request_key=hashlib.sha256(b'["chat","call-1"]').hexdigest(),
                                request_hash='0'*64,period='2026-11',state='complete',timeout_seconds=10,lease_until=stamp)
        db.add(execution)
        db.flush()
        data={'session_id':'session','files':[{'id':'file','name':'report.docx'}]}
        task=OfficeTask(id=execution.id,environment_id=user.id,conversation_id='chat',state='complete',payload={},result=data)
        db.add(task)
        db.flush()
        assert completed_word_export(db,user,req,stamp)
        assert not completed_word_export(db,SimpleNamespace(id='other-user'),req,stamp)
        assert not completed_word_export(db,user,req,stamp+timedelta(hours=1))
        for bad in ({**data,'stderr':'failure'},{**data,'_output_expires_at':(stamp-timedelta(minutes=1)).isoformat()},
                    {'files':[]},{'files':[{'id':'x','name':'x.docx'}]}):
            task.result=bad
            db.flush()
            assert not completed_word_export(db,user,req,stamp)
