"""Synthetic fixtures only; never substitutes fixture data for project measurements."""
from __future__ import annotations
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from step8_finish.finalize_step8 import (collect_evidence, docker_container_test,
    forbidden_path, safe_for_pr, report_md, SECRET_PATTERNS, GROUPS)
from step8_finish.patch_docker_source import patch as patch_docker, OLD, NEW


def save(root,rel,obj):
    p=root/rel;p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(obj),encoding='utf-8')
    return p


def fixture(root):
    dataset=save(root,'data/ml-results/native/step2-network-approved/verified_network_dataset.json',{'synthetic_test':True})
    hashes={}
    for group in GROUPS:
        p=root/f'data/ml-results/native/step3-network-models/synthetic-{group}.pkl'
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_bytes(('fixture-'+group).encode())
        hashes[group]={'artifact_path':str(p.relative_to(root)).replace('\\','/'),
                       'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
    models={g:{'test_rows':34,'verified_healthy_rows':0,'fault_recall_including_abstentions_as_misses':0.5,
               'fault_f1_including_abstentions_as_misses':2/3,'accuracy_including_abstentions_as_errors':0.5,
               'abstentions':17} for g in GROUPS}
    s6={'status':'STEP6_ANALYSIS_COMPLETED_WITH_UNESTIMABLE_GROUND_TRUTH_METRICS',
        'automatic_mitigation_enabled':False,'global_healthy_false_alarm_rate':None,
        'source_integrity':{'dataset_rows':158,'episode_count':10,
                            'episode_counts':{'network_degradation':6,'healthy':4},
                            'dataset_sha256':hashlib.sha256(dataset.read_bytes()).hexdigest(),
                            'test_rows':34,'split':{'train':6,'validation':1,'test':2,'excluded_for_embargo':1},
                            'model_artifacts':hashes},
        'heldout':{'paired_identical_heldout_snapshot_keys':True,'models':models,'episode_results':[]},
        'prospective':{'unique_snapshots':7,'span_seconds':332.6,'safe_hold_verified':True,
                       'ground_truth_labels':False,'trained_predictions':21,'abstentions':0}}
    s7={'status':'STEP7_LOCAL_AUTHENTICATED_SERVICE_VERIFIED_SHADOW_ONLY',
        'authentication_rejects_unauthorized':True,'all_three_model_groups_returned':True,
        'independent_reviews_valid':True,'no_execution':True,
        'model_statuses':{g:'trained_prediction' for g in GROUPS}}
    save(root,'data/ml-results/native/step6-network-validation/STEP6_REPORT.json',s6)
    save(root,'data/ml-results/native/step7-shadow-handoff/STEP7_LOCAL_SMOKE.json',s7)
    return s6,s7


class Step8SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.s6,self.s7=fixture(self.root)

    def test_evidence_verified_from_existing_original_files(self):
        s6,s7=collect_evidence(self.root)
        self.assertEqual(s6['source_integrity']['episode_count'],10)
        self.assertEqual(s7['status'],'STEP7_LOCAL_AUTHENTICATED_SERVICE_VERIFIED_SHADOW_ONLY')

    def test_mutated_original_model_fails_closed(self):
        p=self.root/'data/ml-results/native/step3-network-models/synthetic-aws_only.pkl'
        p.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):
            collect_evidence(self.root)

    def test_missing_healthy_groundtruth_not_promoted(self):
        p=self.root/'data/ml-results/native/step6-network-validation/STEP6_REPORT.json'
        data=json.loads(p.read_text())
        data['heldout']['models']['aws_only']['verified_healthy_rows']=2
        p.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'healthy holdout'):
            collect_evidence(self.root)

    def test_prospective_replay_not_accepted_as_verified(self):
        p=self.root/'data/ml-results/native/step6-network-validation/STEP6_REPORT.json'
        data=json.loads(p.read_text())
        data['prospective']['safe_hold_verified']=False
        p.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'Prospective'):
            collect_evidence(self.root)

    def test_shadow_no_execution_gate(self):
        p=self.root/'data/ml-results/native/step7-shadow-handoff/STEP7_LOCAL_SMOKE.json'
        data=json.loads(p.read_text());data['no_execution']=False
        p.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'safety review'):
            collect_evidence(self.root)

    def test_private_git_paths_are_excluded(self):
        for name in ('data/raw-secret.json','data/ml-results/native/models/a.pkl',
                     '.env','ml/team_starter/credentials.txt','docs/ml/private.key'):
            self.assertFalse(safe_for_pr(name),name)
            self.assertTrue(forbidden_path(name),name)
        self.assertTrue(safe_for_pr('ml/team_starter/shadow.py'))
        self.assertTrue(safe_for_pr('docs/ml/NETWORK_DEGRADATION_FINAL_EVALUATION.md'))
        self.assertFalse(safe_for_pr('scripts/deploy_sqs_terraform.sh'))

    def test_secret_signature_scan(self):
        sample='AKIA'+'Q'*16
        self.assertTrue(any(p.search(sample) for p in SECRET_PATTERNS))
        self.assertFalse(any(p.search('secretKeyRef: Kubernetes Secret name') for p in SECRET_PATTERNS))

    def test_report_cannot_hide_missing_ground_truth(self):
        git={'branch':'feature/ml-team-starter-20261104'}
        doc=report_md(self.s6,self.s7,git,{'status':'PASS','summary':'100 passed'},
                      {'image_id':'sha256:'+'a'*64},
                      {'status':'EPHEMERAL_DOCKER_SERVICE_VERIFIED'})
        for term in ('false alarms/day','not estimable','External','NOT VERIFIED','6',
                     'shadow-only','158','50.0%'):
            self.assertIn(term.lower(),doc.lower())

    def test_docker_source_patch_is_idempotent(self):
        p=self.root/'Dockerfile'
        p.write_text('RUN '+OLD+'\n')
        self.assertTrue(patch_docker(p))
        self.assertFalse(patch_docker(p))
        self.assertIn(NEW,p.read_text())

    def test_unexpected_docker_source_not_clobbered(self):
        p=self.root/'Dockerfile'
        p.write_text('RUN echo custom\n')
        with self.assertRaisesRegex(ValueError,'unexpected format'):
            patch_docker(p)
        self.assertEqual(p.read_text(),'RUN echo custom\n')

    def test_docker_not_accessible_never_claims_pass(self):
        result=docker_container_test({'status':'LOCAL_DOCKER_IMAGE_NOT_CHECKED','image_id':None})
        self.assertEqual(result['status'],'NOT_VERIFIED')


if __name__=='__main__':unittest.main()
