import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,Mock

os.environ.setdefault('HEARTBEAT','0')
import agent
import guardrails
import dol_reversal_live
import continuation_live
import continuation_shadow
from test_execution_policy import env,signal
from test_continuation_live import ContinuationLiveTests


class IsolationTests(unittest.TestCase):
    def test_central_executor_cannot_send_legacy_or_manual_test_order(self):
        with patch.dict(os.environ,env()),patch('requests.post') as post:
            for x in ({'dir':'LONG','entry':100,'SL':90,'TP':120},dict(signal(),_strat='A/B')):
                self.assertFalse(agent._exec_order(x)['sent'])
            post.assert_not_called()

    def test_sibling_batch_rejected_before_reservation_and_rollback(self):
        with patch.dict(os.environ,env()),patch.object(guardrails,'begin_sibling_batch') as reserve,patch('requests.post') as post:
            result=agent._exec_sibling_batch([dict(signal(),_strat='A/B')],'')
            self.assertFalse(result[0]);reserve.assert_not_called();post.assert_not_called()

    def test_actual_bracket_request_still_uses_fixed_geometry_and_has_no_fake_fill(self):
        response=Mock(status_code=200,text='{"success":true}')
        response.json.return_value={'success':True}
        with (patch.dict(os.environ,dict(env(),EXEC_WEBHOOK='https://example.invalid',AB_V3_MODE='SHADOW')),
             patch.object(agent.live_emit,'size_for_budget',return_value=(2,40.,20.)),
             patch.object(guardrails,'_exec_route_id',return_value='TEST-ONLY'),
             patch('requests.post',return_value=response) as post):
            result=agent._exec_order(signal())
        self.assertTrue(result['sent'])
        payload=post.call_args.kwargs['json']
        self.assertEqual(payload['orderType'],'limit')
        self.assertEqual(payload['stopLoss']['stopPrice'],90.)
        self.assertEqual(payload['takeProfit']['limitPrice'],120.)
        self.assertNotIn('fill_ms',result)

    def test_http_200_with_rejection_or_ambiguous_body_is_not_sent(self):
        for body,unknown in (({'success':False},False),({},True)):
            response=Mock(status_code=200,text='test');response.json.return_value=body
            with (patch.dict(os.environ,dict(env(),EXEC_WEBHOOK='https://example.invalid',AB_V3_MODE='SHADOW')),
                  patch.object(agent.live_emit,'size_for_budget',return_value=(2,40.,20.)),
                  patch('requests.post',return_value=response) as post):
                result=agent._exec_order(signal())
            self.assertFalse(result['sent']);self.assertEqual(result['submission_unknown'],unknown)
            self.assertEqual(post.call_count,1)

    def test_full_manager_release_is_not_unlocked(self):
        with patch.dict(os.environ,dict(env(),AB_V3_MODE='LIVE')),patch('requests.post') as post:
            self.assertIn('release_not_live_validated',agent.ab_v3_live.setting_blockers())
            self.assertFalse(agent._exec_order(signal())['sent'])
            agent.ab_v3_live.dispatch_exits();post.assert_not_called()

    def test_dispatcher_rechecks_shadow_before_guard_and_reservation(self):
        order=dict(order_id='TEST',candidate_id='TEST',dol_id='TEST',direction='LONG',
                   activation_ms=1700000000000,entry_price=100,stop_price=90,target_price=120)
        with (patch.dict(os.environ,env()),patch.object(guardrails,'guard_ok') as gate,
             patch.object(guardrails,'note') as note,patch.object(guardrails,'begin_sibling_batch') as reserve):
            result=agent._dispatch_continuation_live(order)
        self.assertEqual(result['state'],'SHADOW');gate.assert_not_called();reserve.assert_not_called()
        self.assertEqual(note.call_args.args[1],'shadow')

    def test_dol_cannot_relabel_live_when_old_environment_says_live(self):
        with patch.dict(os.environ,dict(env(),DOL_REVERSAL_MODE='LIVE')):
            self.assertFalse(dol_reversal_live._live_entry())
            self.assertFalse(dol_reversal_live.dol_reversal_control.readiness()['live_activation_allowed'])

    def test_directional_entry_still_respects_every_guard_refusal(self):
        import time
        stamp=int(time.time()*1000)
        order=dict(strategy='AB_DIRECTIONAL',order_id='TEST',candidate_id='TEST',dol_id='TEST',direction='LONG',
                   activation_ms=stamp,expiry_ms=stamp+600000,entry_price=100.,stop_price=90.,target_price=120.)
        for reason in ('session:ASIA','news_window','news_cal_stale','day_loss_n','day_loss_usd',
                       'loss_streak_n','projected_dd_risk','target_reached','position_open','equity_stale','stale_data'):
            with (self.subTest(reason=reason),
                  patch.dict(os.environ,dict(env(),AB_V3_MODE='SHADOW',ACCOUNT='50000')),
                  patch.object(guardrails,'account_profile',return_value={'plan':'builder50','config_ok':True,'label':'TEST'}),
                  patch.object(guardrails,'exec_mode',return_value='auto'),
                  patch.object(guardrails,'ramp_qty'),patch.object(guardrails,'note'),
                  patch.object(guardrails,'guard_ok',return_value=(False,reason)) as gate,
                  patch.object(guardrails,'begin_sibling_batch') as reserve,
                  patch.object(agent,'_exec_order') as execute,
                  patch.object(agent,'_cal_age_h',return_value=1.),
                  patch.object(agent,'_feed_age_min',return_value=0.),
                  patch.object(agent,'_market_open_now',return_value=True),
                  patch.object(agent,'flags_for',return_value=([],False))):
                result=agent._dispatch_continuation_live(order)
            self.assertEqual(result['state'],'BLOCKED');self.assertEqual(result['reason'],reason)
            gate.assert_called_once();reserve.assert_not_called();execute.assert_not_called()

    def test_shadow_siblings_do_not_read_or_reserve_guard_capacity(self):
        x=dict(signal(),model='reversal',_strat='A/B',_v3_directional=False)
        with (patch.dict(os.environ,dict(env(),AB_SHALLOW_ENABLED='1',SETUP_GROUP_RISK_USD='500')),
             patch.object(agent.ab_shallow.ab_risk_config,'SHALLOW_RISK_SHARE',0.5),
             patch.object(agent,'_signal_bar_close',return_value=105),
             patch.object(guardrails,'setup_group_risk_capacity') as capacity,
             patch.object(guardrails,'begin_sibling_batch') as reserve):
            rows=agent._prepare_shadow_siblings(x)
        self.assertEqual([r['_strat'] for r in rows],['A/B','A/B-shallow'])
        capacity.assert_not_called();reserve.assert_not_called()

    def test_shadow_losses_never_change_guard_day_streak_ramp_or_account_net(self):
        with (tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,env()),
             patch.object(guardrails,'GLOG',str(Path(tmp)/'log.json')),
             patch.object(guardrails,'GSTATE',str(Path(tmp)/'state.json')),
             patch.object(guardrails,'DATA_DIR',tmp),
             patch.object(guardrails,'_shadow_by_key',return_value={}),
             patch.object(guardrails,'_actualize',side_effect=lambda g,s:dict(g,outcome='loss',net=-500))):
            before=guardrails._state()
            guardrails.note(dict(signal(),_strat='A/B'),'shadow','shadow_strategy:V3_ONLY')
            guardrails.note(dict(signal(),_strat='A/B'),'shadow','shadow_strategy:V3_ONLY')
            self.assertEqual(len(guardrails._load(guardrails.GLOG,[])),1)
            self.assertEqual(guardrails._state(),before)
            self.assertEqual(guardrails._day_stats()['net'],0)
            self.assertEqual(guardrails._day_stats()['sent'],0)
            self.assertEqual(guardrails._loss_streak_stats()['streak'],0)
            self.assertEqual(guardrails.trade_summary(guardrails._load(guardrails.GLOG,[]))['sent_orders'],0)


class DrainPolicyTests(ContinuationLiveTests):
    def test_armed_continuation_never_calls_dispatcher_under_v3_only(self):
        self._bar(1000000);dispatch=Mock();continuation_live.configure(dispatch);continuation_live.drain()
        self._order(1060000);self._bar(1060000)
        with patch.dict(os.environ,dict(env(),CONTINUATION_LIVE_LONG='1')):
            result=continuation_live.drain()
        self.assertEqual(result['results'][0]['state'],'SHADOW');dispatch.assert_not_called()


if __name__=='__main__':unittest.main()
