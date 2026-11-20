import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from ai_worker import ChatWorker
from app import SURVIVEXApp
from places import PlacesDatabase
from wokwi.display_adapter import compact, pack_ops


class OptimizationTests(unittest.TestCase):
    def test_reuse_crash_idle_and_budget(self):
        worker=ChatWorker(server='/usr/bin/true',model=str(Path(__file__).resolve()),idle_timeout=.1)
        process=MagicMock();process.poll.return_value=None
        sock=MagicMock();sock.__enter__.return_value.getsockname.return_value=('127.0.0.1',1234)
        calls=[];token_count=[1]
        def urlopen(req,timeout):
            url=req if isinstance(req,str) else req.full_url
            calls.append(url)
            value=({'status':'ok'} if url.endswith('/health') else {'prompt':'x'} if url.endswith('/apply-template') else {'tokens':[1]*token_count[0]} if url.endswith('/tokenize') else {'choices':[{'finish_reason':'stop','message':{'content':'OK'}}]})
            result=MagicMock();result.__enter__.return_value=io.BytesIO(json.dumps(value).encode());return result
        with patch('ai_worker.socket.socket',return_value=sock),patch('ai_worker.subprocess.Popen',return_value=process) as spawn,patch('ai_worker.urllib.request.urlopen',side_effect=urlopen):
            for prompt in ('one','two'):
                self.assertTrue(worker.submit(prompt));worker.thread.join(2);self.assertEqual(worker.status,'READY',worker.error)
            self.assertEqual(spawn.call_count,1)
            process.poll.return_value=1
            self.assertTrue(worker.submit('three'));worker.thread.join(2)
            self.assertEqual(spawn.call_count,2)
            process.poll.return_value=None
            token_count[0]=1000
            self.assertTrue(worker.submit('oversized'));worker.thread.join(2)
            self.assertEqual(worker.status,'ERROR')
            self.assertNotIn('oversized',worker.history)
            worker.close()
        worker2=ChatWorker(server='/usr/bin/true',model=str(Path(__file__).resolve()),idle_timeout=.05)
        with patch('ai_worker.socket.socket',return_value=sock),patch('ai_worker.subprocess.Popen',return_value=process),patch('ai_worker.urllib.request.urlopen',side_effect=urlopen):
            token_count[0]=1;worker2.submit('one');worker2.thread.join(2);time.sleep(.15)
            self.assertIsNone(worker2.process)
        worker2.close()

    def test_packing_matches_original(self):
        def original(ops):
            groups=[];group=[]
            for op in ops:
                if len(compact(['b',*group,op]).encode())>160:
                    if group:groups.append(compact(['b',*group]) if len(group)>1 else compact(group[0]))
                    group=[op]
                else:group.append(op)
            if group:groups.append(compact(['b',*group]) if len(group)>1 else compact(group[0]))
            return groups
        ops=[['t',0,0,1,123,'x'*i] for i in range(1,100)]
        self.assertEqual(pack_ops(ops),original(ops))

    def test_database_switch_and_cache_invalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths=[Path(tmp)/'a',Path(tmp)/'b']
            for i,path in enumerate(paths):
                db=PlacesDatabase(path);db.set_coverage('Region'+str(i),0,2,0,2,'test');db.close()
            ui=SURVIVEXApp(str(Path(tmp)/'wp'),str(paths[0]))
            try:
                req=('', 'All', (1,1),None)
                ui.search_in_worker(paths[0],req);old=ui.worker_places_db
                ui.search_in_worker(paths[0],req);self.assertIs(old,ui.worker_places_db)
                ui.search_in_worker(paths[1],req);self.assertIsNot(old,ui.worker_places_db)
                self.assertEqual(ui.worker_places_db.get_coverage().region_name,'Region1')
            finally:ui.close_worker_places();ui.close()
