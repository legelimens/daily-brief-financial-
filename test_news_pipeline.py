from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import brief
import news_pipeline as p


def scored(url='https://example.org/a', key='org-release-v1', region='cn', score=8, track='ai'):
    return brief.ScoredItem(title='Title', url=url, summary_raw='', published=datetime.now(timezone.utc),
        source_name='Source', region=region, track_hint=track, track=track, score=score,
        summary_cn='Fact', position_cn='', angle_cn='Analysis', tier='一手', event_key=key)


def test_history_survives_restart_and_allows_new_development(tmp_path):
    path = tmp_path/'sent.json'
    a = scored()
    p.save_history(path, [], [a])
    history = p.read_history(path)
    assert p.select_new([scored(url='https://other.org/translation'), a], history) == []
    update = scored(url='https://other.org/new', key='org-release-v2')
    assert p.select_new([update], history) == [update]


def test_corrupted_history_does_not_reset(tmp_path):
    path = tmp_path/'sent.json'
    path.write_text('{broken')
    with pytest.raises(ValueError):
        p.read_history(path)


def test_fractional_timestamp_and_chinese_date():
    assert p.parse_date('2026-09-15T01:02:03.123Z').microsecond == 123000
    assert p.parse_date('2026.09.15', 8).utcoffset() == timedelta(hours=8)


def test_render_escapes_source_content_and_labels_missing_regions(tmp_path):
    item = scored()
    item.title = '<script>alert(1)</script>'
    digest = brief.Briefing('2026-09-15', 'Preview', 'ai', {'ai':[item], 'data':[]}, {'ai':1,'data':0})
    html = brief.render_html(digest, {'output':{'dir':str(tmp_path)}})
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert '数据 · 国内：本次无合格入选条目' in html


def test_history_expires(tmp_path):
    import json
    path = tmp_path/'sent.json'
    path.write_text(json.dumps([dict(url='old', event_key='old', sent_at=(datetime.now(timezone.utc)-timedelta(days=15)).isoformat())]))
    assert p.read_history(path) == []


def test_region_selection_preserves_quality_and_both_regions():
    items = [scored(url=str(i), key=str(i), region='global', score=9) for i in range(8)]
    items += [scored(url='cn'+str(i), key='cn'+str(i), score=7) for i in range(2)]
    config = {'filtering': {'max_per_track': {'ai':6,'data':6}, 'min_per_region':2}}
    digest = brief.build_briefing(items, config, dry_run=True)
    assert len(digest.groups['ai']) == 6
    assert sum(i.region == 'cn' for i in digest.groups['ai']) == 2
    assert digest.groups['data'] == []


def test_collector_ignores_updated_old_undated_and_future(monkeypatch):
    now = datetime(2026, 9, 15, 6, tzinfo=timezone.utc)
    def entry(pub=None, updated=None):
        return dict(title='Title', link='https://example.org/a', summary='body '*50,
                    published=pub, updated=updated)
    entries = [entry('2026-09-15T01:00:00Z'), entry('2026-09-10', '2026-09-15'),
               entry(updated='2026-09-15'), entry('2026-09-16')]
    response = SimpleNamespace(content=b'', raise_for_status=lambda:None)
    class Client:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def get(self,*args,**kwargs): return response
    monkeypatch.setattr(p, 'session', Client)
    monkeypatch.setattr(p.feedparser, 'parse', lambda _:SimpleNamespace(entries=entries))
    rows, report = p.collect_one(dict(name='test', url='https://example.org/feed'), 'ai', now, 24)
    assert len(rows) == 1
    assert report['undated'] == 1 and report['future'] == 1


def test_all_dropped_is_valid_analysis(monkeypatch):
    monkeypatch.setattr(brief, '_chat_completion', lambda *a,**k:'[{"id":1,"track":"drop"}]')
    config = {'filtering': {'score_threshold':6}}
    item = brief.Item('Title','https://example.org','',None,'test','cn','ai')
    assert brief.score_and_enrich([item], config) == []
    assert config['_analysis']['processed'] == 1


def test_missing_model_result_is_incomplete(monkeypatch):
    monkeypatch.setattr(brief, '_chat_completion', lambda *a,**k:'[]')
    config = {'filtering': {'score_threshold':6}}
    item = brief.Item('Title','https://example.org','',None,'test','cn','ai')
    brief.score_and_enrich([item], config)
    assert config['_analysis']['processed'] == 0


def test_preview_does_not_write_sent_history(tmp_path, monkeypatch):
    monkeypatch.setattr(brief, '_load_dotenv', lambda:None)
    config = {'filtering':{'time_window_hours':24,'score_threshold':6,'max_per_track':6},
              'sources':{},'output':{'dir':str(tmp_path/'output')},'history':{'path':str(tmp_path/'sent.json')}}
    monkeypatch.setattr(brief,'load_config',lambda _:config)
    monkeypatch.setattr(brief.sys,'argv',['brief.py','--no-email'])
    monkeypatch.setattr(p,'collect',lambda *a:([],[],datetime.now(timezone.utc)))
    monkeypatch.setattr(brief,'send_email',lambda *a:pytest.fail('Preview attempted email'))
    assert brief.main() == 0
    assert not (tmp_path/'sent.json').exists()


@pytest.mark.parametrize('fail', [False, True])
def test_only_successfully_sent_items_enter_history(tmp_path, monkeypatch, fail):
    item = scored()
    config = {'filtering':{'time_window_hours':24,'score_threshold':6,'max_per_track':6},
        'sources':{}, 'output':{'dir':str(tmp_path/'output')},
        'history':{'path':str(tmp_path/'sent.json')}, 'email':{'enabled':True}}
    monkeypatch.setattr(brief, '_load_dotenv', lambda:None)
    monkeypatch.setattr(brief, 'load_config', lambda _:config)
    monkeypatch.setattr(brief.sys, 'argv', ['brief.py'])
    monkeypatch.setattr(p, 'collect', lambda *a:([],[],datetime.now(timezone.utc)))
    def score(*args):
        config['_analysis'] = {'processed':0}
        return [item]
    monkeypatch.setattr(brief, 'score_and_enrich', score)
    monkeypatch.setattr(brief, '_chat_completion', lambda *a,**k:None)
    def send(*args):
        if fail:
            raise RuntimeError('SMTP failed')
    monkeypatch.setattr(brief, 'send_email', send)
    assert brief.main() == (1 if fail else 0)
    assert (tmp_path/'sent.json').exists() is not fail


def test_account_error_stops_later_batches(monkeypatch):
    calls = []
    def unavailable(config,*args,**kwargs):
        calls.append(True)
        config['_llm_fatal'] = True
        return None
    monkeypatch.setattr(brief,'_chat_completion',unavailable)
    item = brief.Item('Title','https://example.org','',None,'test','cn','ai')
    brief.score_and_enrich([item]*20, {'filtering':{'score_threshold':6}})
    assert len(calls) == 1


@pytest.mark.parametrize('finish,content,expected', [('stop','{"ok":true}','{"ok":true}'), ('length','partial',None), ('stop','',None)])
def test_glm_payload_and_incomplete_response(monkeypatch, finish, content, expected):
    captured = {}
    class Client:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def post(self,endpoint,**kwargs):
            captured.update(kwargs['json'])
            return SimpleNamespace(status_code=200,json=lambda:{'choices':[{'finish_reason':finish,'message':{'content':content}}]})
    monkeypatch.setenv('TEST_GLM_KEY','not-a-real-key')
    monkeypatch.setattr(p,'session',Client)
    config={'ai':{'base_url':'https://example.org/v4','model':'glm-4.7-flash','api_key_env':'TEST_GLM_KEY','thinking':'disabled'}}
    assert brief._chat_completion(config,[],100) == expected
    assert captured['thinking'] == {'type':'disabled'}
    assert captured['model'] == 'glm-4.7-flash'


def test_configured_batch_size(monkeypatch):
    counts=[]
    def answer(config,messages,**kwargs):
        count=messages[1]['content'].count('标题：')
        counts.append(count)
        import json
        return json.dumps([{'id':i+1,'track':'drop'} for i in range(count)])
    monkeypatch.setattr(brief,'_chat_completion',answer)
    item=brief.Item('Title','https://example.org','',None,'test','cn','ai')
    brief.score_and_enrich([item]*11,{'ai':{'batch_size':5},'filtering':{'score_threshold':6}})
    assert counts == [5,5,1]


def test_glm_busy_retries_then_stops_without_changing_model(monkeypatch):
    calls=[]
    delays=[]
    class Client:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def post(self,*args,**kwargs):
            calls.append(kwargs['json']['model'])
            return SimpleNamespace(status_code=429,headers={'Retry-After':'30'},json=lambda:{'error':{'code':'1305','message':'busy'}})
    monkeypatch.setenv('TEST_GLM_KEY','not-a-real-key')
    monkeypatch.setattr(p,'session',Client)
    monkeypatch.setattr(brief.time,'sleep',lambda seconds:delays.append(seconds))
    config={'ai':{'base_url':'https://example.org','model':'glm-4.7-flash','api_key_env':'TEST_GLM_KEY'}}
    assert brief._chat_completion(config,[],100) is None
    assert config['_llm_fatal'] is True
    assert calls == ['glm-4.7-flash']*3
    assert delays == [30,30]


def test_supplements_never_displace_recent_news_and_have_higher_threshold():
    items=[scored(url=str(i),key=str(i),score=6) for i in range(3)]
    for item in items:
        item.published = items[0].published
    older=scored(url='older',key='older',score=10)
    older.supplemental=True
    weak=scored(url='weak',key='weak',score=6)
    weak.supplemental=True
    config={'filtering':{'max_per_track':4,'supplement_max_per_track':2,'supplement_score_threshold':6.5}}
    digest=brief.build_briefing(items+[older,weak],config,dry_run=True)
    assert digest.groups['ai']==items+[older]
    config['filtering']['max_per_track']=3
    assert brief.build_briefing(items+[older],config,dry_run=True).groups['ai']==items


def test_recent_version_wins_over_older_higher_score_duplicate():
    recent=scored(score=6)
    older=scored(url='https://example.org/old',score=10)
    older.supplemental=True
    assert p.select_new([older,recent],[])==[recent]


def test_supplement_cap_and_rendering(tmp_path):
    items=[scored(url=str(i),key=str(i),score=8) for i in range(5)]
    for item in items:item.supplemental=True
    digest=brief.build_briefing(items,{'filtering':{'max_per_track':8,'supplement_max_per_track':2}},dry_run=True)
    assert len(digest.groups['ai'])==2
    page=brief.render_html(digest,{'output':{'dir':str(tmp_path)}})
    assert '重要补充 · 24–48 小时' in page


def test_truncated_batch_retries_and_completed_analysis_is_cached(tmp_path, monkeypatch):
    import json
    calls=[]
    def answer(config,messages,max_tokens):
        calls.append(max_tokens)
        config['_response_truncated']=max_tokens==4800
        if max_tokens==4800:return None
        return json.dumps([{'id':1,'track':'drop'}])
    monkeypatch.setattr(brief,'_chat_completion',answer)
    config={'ai':{'analysis_cache_dir':str(tmp_path/'cache')},'filtering':{'score_threshold':5.5}}
    item=brief.Item('Title','https://example.org','',None,'test','cn','ai')
    assert brief.score_and_enrich([item],config)==[]
    assert calls==[4800,9600]
    assert config['_analysis']['processed']==1
    brief.score_and_enrich([item],config)
    assert calls==[4800,9600]
    assert config['_analysis']['processed']==1


def test_truncated_batch_split_preserves_original_ids(monkeypatch):
    import json
    def answer(config,messages,max_tokens):
        count=messages[1]['content'].count('标题：')
        config['_response_truncated']=count>1
        return None if count>1 else json.dumps([{'id':1,'track':'drop'}])
    monkeypatch.setattr(brief,'_chat_completion',answer)
    item=brief.Item('Title','https://example.org','',None,'test','cn','ai')
    result=json.loads(brief._score_batch_content([item]*5,{}))
    assert [r['id'] for r in result]==[1,2,3,4,5]


def test_roundup_is_not_analyzed_as_single_event(monkeypatch):
    import json
    def answer(config,messages,max_tokens):
        assert '早报｜' not in messages[1]['content']
        return '[{"id":1,"track":"drop"}]'
    monkeypatch.setattr(brief,'_chat_completion',answer)
    items=[brief.Item(title,'https://example.org','',None,'test','cn','ai') for title in ['早报｜事件甲/事件乙','独立新闻']]
    assert [r['id'] for r in json.loads(brief._score_batch_content(items,{}))]==[1,2]


def test_editorial_review_merges_cross_track_event_and_corrects_topic(tmp_path, monkeypatch):
    a=scored(url='a',key='a',track='ai')
    b=scored(url='b',key='b',track='data')
    monkeypatch.setattr(brief,'_chat_completion',lambda *a,**kw:'{"duplicates":[{"keep":0,"remove":[1]}],"reclassify":[{"id":0,"track":"data"}]}')
    result=brief.editorial_review([a,b],{'filtering':{'editorial_review':True},'output':{'dir':str(tmp_path)}})
    assert result==[a] and a.track=='data'


def test_editorial_review_rejects_unknown_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(brief,'_chat_completion',lambda *a,**kw:'{"duplicates":[{"keep":0,"remove":[99]}]}')
    with pytest.raises(ValueError):
        brief.editorial_review([scored(),scored(url='b')],{'filtering':{'editorial_review':True},'output':{'dir':str(tmp_path)}})


def test_test_email_receipt_suppresses_only_accepted_matching_recipient(tmp_path):
    import hashlib,json
    markup='<a class="news-title" href="https://example.org/already-sent">News</a>'
    page=tmp_path/'sent.html';page.write_text(markup,encoding='utf-8')
    receipt={'status':'accepted_by_smtp','recipient':'test@example.org','html':str(page),'completed_at':datetime.now(timezone.utc).isoformat(),'sha256':hashlib.sha256(markup.encode()).hexdigest()}
    (tmp_path/'test_delivery_test.json').write_text(json.dumps(receipt),encoding='utf-8')
    assert p.read_test_deliveries(tmp_path,['other@example.org'])==[]
    assert p.read_test_deliveries(tmp_path,['test@example.org'])[0]['url']=='https://example.org/already-sent'
    page.write_text('modified',encoding='utf-8')
    assert p.read_test_deliveries(tmp_path,['test@example.org'])==[]
