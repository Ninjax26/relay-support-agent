import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
from server import create_server
from supportops.store import Store
from test_agent import FakePlanner, PLAN, BILLING
from test_resume import ENV


class ResumeAPITests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, ENV)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = create_server(port=0, store=Store(Path(self.temp.name) / 'resume.sqlite3'), start_workers=False, capacity=1)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, body=None):
        req = urllib.request.Request(self.base+path, data=json.dumps(body).encode() if body is not None else None, headers={'Content-Type':'application/json'})
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def failed_live(self):
        _, queued = self.request('/api/runs', {'message':'Charged twice','mode':'live'})
        first = FakePlanner([('update_plan',PLAN),('search_knowledge',{'query':'duplicate payment'}),('lookup_account',{})])
        respond = first.respond
        def failure(history):
            if len(first.histories) == 3:
                raise RuntimeError('Simulated connection failure')
            result = respond(history)
            result['output'][0]['_gemini_message'] = {'opaque':'private-metadata-marker'}
            return result
        first.respond = failure
        with patch('supportops.agent.GeminiPlanner',return_value=first):
            self.server.jobs.run_one()
        return self.request('/api/runs/'+queued['id'])[1]

    def test_http_resume_keeps_id_and_hides_private_history(self):
        failed = self.failed_live()
        self.assertTrue(failed['resume_available'])
        self.assertNotIn('private-metadata-marker',json.dumps(failed))
        self.assertNotIn('private-metadata-marker',json.dumps(self.request('/api/runs')[1]))
        with self.server.store.connect() as db:
            self.assertIn('private-metadata-marker',db.execute('SELECT state FROM agent_checkpoints').fetchone()[0])
        status,resumed = self.request(f'/api/runs/{failed["id"]}/resume',{'request_id':'resume-one','history':['untrusted client content']})
        self.assertEqual(status,202)
        self.assertEqual(resumed['id'],failed['id'])
        second = FakePlanner([('verify_case',BILLING),('complete_case',BILLING)])
        with patch('supportops.agent.GeminiPlanner',return_value=second):
            self.server.jobs.run_one()
        finished = self.request('/api/runs/'+failed['id'])[1]
        self.assertEqual(finished['status'],'awaiting_approval')
        self.assertEqual(finished['resume_count'],1)
        self.assertFalse(finished['resume_available'])
        self.assertNotIn('private-metadata-marker',json.dumps(finished))
        self.assertEqual(self.request(f'/api/runs/{failed["id"]}/resume',{'request_id':'resume-one'})[0],202)
        self.assertEqual(self.request(f'/api/runs/{failed["id"]}/resume',{'request_id':'new-key'})[0],400)
        self.assertEqual(self.server.store.metrics()['tickets'],0)
        with self.server.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM agent_checkpoints').fetchone()[0],0)

    def test_resume_rejects_changed_config_and_full_queue(self):
        failed = self.failed_live()
        with patch.dict(os.environ,{'GEMINI_MODEL':'different-model'}):
            self.assertEqual(self.request(f'/api/runs/{failed["id"]}/resume',{'request_id':'changed'})[0],400)
        self.request('/api/runs',{'message':'API timeout','mode':'demo'})
        self.assertEqual(self.request(f'/api/runs/{failed["id"]}/resume',{'request_id':'full'})[0],429)
        self.assertEqual(self.request('/api/runs/'+failed['id'])[1]['status'],'failed')

    def test_corrupt_checkpoint_and_active_or_demo_run_fail_closed(self):
        _,demo = self.request('/api/runs',{'message':'API timeout'})
        self.assertEqual(self.request(f'/api/runs/{demo["id"]}/resume',{})[0],400)
        self.server.jobs.run_one()
        failed = self.failed_live()
        with self.server.store.connect() as db:
            db.execute('UPDATE agent_checkpoints SET state=?', ('{"private-data":"do-not-expose"}',))
        status,body = self.request(f'/api/runs/{failed["id"]}/resume',{})
        self.assertEqual(status,400)
        self.assertNotIn('do-not-expose',json.dumps(body))
        self.assertEqual(self.request('/api/runs/missing/resume',{})[0],404)


if __name__ == '__main__':
    unittest.main()
