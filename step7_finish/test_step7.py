import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from step7_finish import service, finish_step7


def sample(now=None):
    return {'contract_version':'adaptivenet.features.v1','snapshot_id':'features-abc123',
            'computed_at_unix':(time.time() if now is None else now)-5,
            'ready_for_model':True,'ready_for_automatic_action':False,
            'source_errors':[],'quality_reasons':[]}


def slow(original, mode, now):
    return {'contract_version':'adaptivenet.model_input.v1','snapshot_id':original['snapshot_id'],
            'computed_at_unix':original['computed_at_unix'],'actions_enabled':False}


def fast(events, now):
    return {'contract_version':'adaptivenet.fast_signals.v1','ready_for_fast_assessment':True}


def pred(payload,path):
    return {'contract_version':'adaptivenet.proposal.v1',
            'snapshot_id':payload['slow']['snapshot_id'],'action':'investigate',
            'confidence':0.8,'model_id':str(path),'evidence':['shadow_condition:network_degradation']}


def review(snap,proposal):
    return {'review_version':'adaptivenet.decision_review.v1',
            'policy_valid':True,'approved_for_execution':False,'executed':False,'effective_action':'hold'}


class Step7Security(unittest.TestCase):
    def setUp(self):
        self.paths=dict.fromkeys(service.GROUPS,'local-trusted-model')
        self.request={'original_snapshot':sample(),'http_witness_events':[]}

    def call(self,**kwargs):
        return service.evaluate(self.request,self.paths,predict=kwargs.get('predict',pred),
                 review=kwargs.get('review',review),make_slow=kwargs.get('make_slow',slow),make_fast=fast)

    def test_valid_input_all_three_reviewed_no_actuation(self):
        result=self.call()
        self.assertEqual(set(result['models']),set(service.GROUPS))
        self.assertFalse(result['approved_for_execution'])
        self.assertFalse(result['executed'])
        self.assertEqual(result['effective_action'],'hold')
        self.assertTrue(all(x['independent_review']['policy_valid'] for x in result['models'].values()))

    def test_stale_original_rejected(self):
        self.request['original_snapshot']=sample(time.time()-181)
        with self.assertRaisesRegex(ValueError,'stale'):
            self.call()

    def test_missing_bearer_token_rejected(self):
        self.assertFalse(service.authorized(None,'a'*32))
        self.assertFalse(service.authorized('Bearer bad','a'*32))
        self.assertTrue(service.authorized('Bearer '+'a'*32,'a'*32))

    def test_unsafe_model_action_rejected(self):
        def unsafe(p,model):
            proposal=pred(p,model);proposal['action']='shift_traffic';return proposal
        with self.assertRaisesRegex(ValueError,'unsafe model action'):
            self.call(predict=unsafe)

    def test_reviewer_execution_rejected(self):
        def bad(s,p):
            decision=review(s,p);decision['executed']=True;return decision
        with self.assertRaisesRegex(ValueError,'attempted execution'):
            self.call(review=bad)

    def test_changed_model_snapshot_rejected(self):
        def wrong(orig,mode,now):
            result=slow(orig,mode,now);result['snapshot_id']='features-wrong';return result
        with self.assertRaisesRegex(ValueError,'changed original identity'):
            self.call(make_slow=wrong)

    def test_source_errors_rejected(self):
        self.request['original_snapshot']['source_errors']=['amp_unavailable']
        with self.assertRaisesRegex(ValueError,'not ready'):
            self.call()

    def test_future_witness_rejected(self):
        self.request['http_witness_events']=[{'scope':'region-a','timestamp':self.request['original_snapshot']['computed_at_unix']+10}]
        with self.assertRaisesRegex(ValueError,'future'):
            self.call()

    def test_unknown_request_fields_rejected(self):
        self.request['actions_enabled']=True
        with self.assertRaisesRegex(ValueError,'requires only'):
            self.call()

    def test_hash_integrity_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            f=root/'data/ml-results/native/step4-network-evaluation/step4-evaluation.json'
            f.parent.mkdir(parents=True)
            mod=root/'data/ml-results/native/step3-network-models';mod.mkdir(parents=True)
            models={}
            for group in finish_step7.GROUPS:
                file=mod/(group+'.pkl');file.write_bytes(b'trusted-test-fixture')
                models[group]={'model_id':group,'model_file':str(file),'model_sha256':hashlib.sha256(file.read_bytes()).hexdigest()}
            f.write_text(json.dumps({'evaluation_status':'completed_with_explicit_heldout_healthy_limitation','models':models}))
            self.assertEqual(len(finish_step7.audit_models(root)),3)
            (mod/'aws_only.pkl').write_bytes(b'MODIFIED')
            with self.assertRaisesRegex(ValueError,'hash changed'):
                finish_step7.audit_models(root)

    def test_copy_allowlist_excludes_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            src=Path(d)/'src';src.mkdir()
            (src/'worker.py').write_text('print(1)')
            (src/'.env').write_text('private')
            (src/'creds.json').write_text('private')
            (src/'tests').mkdir();(src/'tests'/'test.py').write_text('pass')
            dst=Path(d)/'dst'
            self.assertEqual(finish_step7.safe_copy_py(src,dst),1)
            self.assertTrue((dst/'worker.py').exists())
            self.assertFalse((dst/'.env').exists())
            self.assertFalse((dst/'creds.json').exists())
            self.assertFalse((dst/'tests').exists())


if __name__=='__main__':unittest.main()

# Additional offline integration of the generated build context (no Docker involved).
class Step7Packaging(unittest.TestCase):
    def test_context_contains_only_expected_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'repo'; root.mkdir()
            step=root/'step7_finish';step.mkdir()
            # Tests only the allowlisted staging function with a minimal stub checkout.
            for name in ('service.py','Dockerfile','requirements-service.txt'):
                (step/name).write_text('content')
            sf=root/'step5_finish';sf.mkdir();(sf/'finalize_step5.py').write_text('content')
            ml=root/'ml';ml.mkdir();(ml/'predictor.py').write_text('content')
            sv=root/'services/adaptivenet/ingestion';sv.mkdir(parents=True);(sv/'handoff.py').write_text('content')
            (root/'.env').write_text('SECRET')
            (root/'data/private.db').parent.mkdir(parents=True,exist_ok=True)
            (root/'data/private.db').write_bytes(b'PRIVATE')
            models={}
            artifact=root/'data/ml-results/native/step3-network-models';artifact.mkdir(parents=True)
            for group in finish_step7.GROUPS:
                filename=f'{group}.pkl';(artifact/filename).write_bytes(group.encode())
                models[group]={'filename':filename,'sha256':finish_step7.sha(artifact/filename),'model_id':group}
            step4=root/'data/ml-results/native/step4-network-evaluation';step4.mkdir(parents=True)
            (step4/'step4-evaluation.json').write_text('{}')
            dest=root/'data/ml-results/native/step7-container-context'
            count=finish_step7.prepare_context(models,root,dest)
            self.assertEqual(count['ml_python_files'],1)
            self.assertFalse((dest/'.env').exists())
            self.assertFalse((dest/'data/private.db').exists())
            self.assertTrue((dest/'ml/predictor.py').is_file())
            self.assertTrue((dest/'data/ml-results/native/step3-network-models/aws_only.pkl').is_file())

class Step7EksMetadataTests(unittest.TestCase):
    def test_checks_fail_closed_on_deployment(self):
        from step7_finish.verify_eks_readonly import check_resources
        self.assertIn('Deployment has no ready replicas',check_resources({}, {}, {}, {}))

    def test_readonly_metadata_gate(self):
        from step7_finish.verify_eks_readonly import check_resources
        dep={'status':{'readyReplicas':1},'spec':{'template':{'spec':{
           'automountServiceAccountToken':False,
           'containers':[{'name':'shadow-inference','image':'registry/app@sha256:abc',
               'securityContext':{'readOnlyRootFilesystem':True,'allowPrivilegeEscalation':False},
               'env':[{'name':'ADAPTIVENET_SHADOW_TOKEN','valueFrom':{'secretKeyRef':{'name':'adaptivenet-shadow-ml-token'}}}]}]}}}}
        svc={'spec':{'type':'ClusterIP'}}
        policy={'spec':{'policyTypes':['Ingress','Egress'],'egress':[]}}
        endpoints={'subsets':[{'addresses':[{'ip':'10.0.0.1'}]}]}
        self.assertEqual(check_resources(dep,svc,policy,endpoints),[])
