import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from server import create_server
from supportops.store import Store
from supportops.jobs import JobRunner

class Phase6Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'runs.sqlite3')
    def submit(self,key='x'):
        return self.store.enqueue({'message':'API timeout','account_id':'acct_demo','mode':'demo'},key)
    def test_queued_cancel_is_terminal_and_idempotent(self):
        run=self.submit()
        first=self.store.cancel_run(run['id'])
        self.assertEqual(first['status'],'cancelled')
        self.assertEqual(self.store.cancel_run(run['id'])['version'],first['version'])
        self.assertIsNone(self.store.claim('worker'))
        with self.assertRaises(ValueError):self.store.decide(run['id'],'approve')
    def test_running_cancel_fences_final_completion(self):
        run=self.submit()
        claimed=self.store.claim('owner')
        self.assertEqual(claimed['id'],run['id'])
        request=self.store.cancel_run(run['id'])
        self.assertTrue(request['cancel_requested'])
        with self.assertRaises(Exception):
            self.store.checkpoint(run['id'],'owner',{'status':'awaiting_approval'},terminal=True)
        final=self.store.finish_cancel(run['id'],'owner')
        self.assertEqual(final['status'],'cancelled')
        self.assertEqual(self.store.metrics()['tickets'],0)
    def test_worker_stops_between_steps(self):
        run=self.submit()
        runner=JobRunner(self.store,agent=None)
        def fake_agent(message,account_id,mode,on_step):
            self.store.cancel_run(run['id'])
            on_step({'trace':[]})
            return {'status':'awaiting_approval'}
        runner.agent=fake_agent
        self.assertTrue(runner.run_one())
        self.assertEqual(self.store.get(run['id'])['status'],'cancelled')
    def test_cancel_during_live_provider_wait_prevents_tool_execution(self):
        import os
        entered=threading.Event(); release=threading.Event()
        class SlowPlanner:
            model='gemini-3.8-flash'
            def respond(self,history):
                entered.set()
                if not release.wait(5): raise RuntimeError('test timeout')
                return {'output':[{'type':'function_call','name':'lookup_account','call_id':'slow','arguments':'{}'}],
                        'usage':{'input_tokens':1,'output_tokens':1}}
        with patch.dict(os.environ,{'AGENT_PROVIDER':'gemini','GEMINI_API_KEY':'test-key','GEMINI_MODEL':'gemini-3.8-flash'}):
            run=self.store.enqueue({'message':'API timeout','account_id':'acct_demo','mode':'live','provider':'gemini'},'live')
            runner=JobRunner(self.store)
            with patch('supportops.agent.GeminiPlanner',return_value=SlowPlanner()):
                worker=threading.Thread(target=runner.run_one);worker.start()
                self.assertTrue(entered.wait(5))
                self.assertTrue(self.store.cancel_run(run['id'])['cancel_requested'])
                release.set();worker.join(5)
                self.assertFalse(worker.is_alive())
            final=self.store.get(run['id'])
            self.assertEqual(final['status'],'cancelled')
            self.assertEqual(final['trace'],[])
            self.assertEqual(self.store.metrics()['tickets'],0)
            with self.store.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM agent_checkpoints').fetchone()[0],0)
    def test_cancel_http_and_stream_transition(self):
        server=create_server(port=0,store=self.store,start_workers=False)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join()))
        run=self.submit('http-cancel')
        conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        conn.request('GET',f'/api/runs/{run["id"]}/events')
        response=conn.getresponse()
        self.assertEqual(response.status,200)
        self.assertIn('"status": "queued"',response.readline().decode()+response.readline().decode())
        request=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        request.request('POST',f'/api/runs/{run["id"]}/cancel',body='{}',headers={'Content-Type':'application/json'})
        done=request.getresponse();self.assertEqual(done.status,200)
        self.assertEqual(json.load(done)['status'],'cancelled')
        request.close()
        lines=[response.readline().decode() for _ in range(3)]
        self.assertTrue(any('"status": "cancelled"' in line for line in lines))
        conn.close()
    def test_sse_first_event_and_reconnect_cursor(self):
        server=create_server(port=0,store=self.store,start_workers=False)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join()))
        run=self.submit()
        connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        connection.request('GET',f'/api/runs/{run["id"]}/events',headers={'Accept':'text/event-stream'})
        response=connection.getresponse()
        self.assertEqual(response.status,200)
        self.assertIn('text/event-stream',response.getheader('Content-Type'))
        lines=[response.readline().decode() for _ in range(3)]
        self.assertTrue(any('id: 1' in line for line in lines))
        self.assertTrue(any('"status": "queued"' in line for line in lines))
        connection.close()
