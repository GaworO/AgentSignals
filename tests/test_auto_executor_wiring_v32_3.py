import os
import subprocess
import sys
import textwrap
import unittest


class AutoExecutorWiringTests(unittest.TestCase):
    def test_auto_executor_uses_frozen_plan_and_keeps_all_trades_route(self):
        script = textwrap.dedent(
            r"""
            import json
            import os
            import sys
            import tempfile
            import types

            tmp = tempfile.TemporaryDirectory()
            os.environ.update({
                'DATA_DIR': tmp.name,
                'HEARTBEAT': '0',
                'CHALLENGE_MODE': '1',
                'ACCOUNT': '100000',
                'RISK_PCT': '0.35',
                'AB_SHALLOW_RISK_PCT': '0.35',
                'EXEC_MAX_QTY': '15',
                'EXEC_WEBHOOK': 'https://existing-relay.invalid/stage',
                'BROKER_FEEDBACK_REQUIRED': '0',
            })

            # The repository's runtime installs Flask from requirements.txt.
            # This isolated wiring test supplies only the tiny registration API
            # it needs so it can also run in dependency-light build sandboxes.
            flask = types.ModuleType('flask')

            class Rule:
                def __init__(self, rule):
                    self.rule = rule

            class URLMap:
                def __init__(self):
                    self.rules = []
                def iter_rules(self):
                    return list(self.rules)

            class Flask:
                def __init__(self, *args, **kwargs):
                    self.url_map = URLMap()
                    self.view_functions = {}
                def route(self, rule, **kwargs):
                    def decorator(fn):
                        self.add_url_rule(rule, fn.__name__, fn, **kwargs)
                        return fn
                    return decorator
                def add_url_rule(self, rule, endpoint, view_func, **kwargs):
                    self.url_map.rules.append(Rule(rule))
                    self.view_functions[endpoint] = view_func
                def run(self, *args, **kwargs):
                    return None

            class Response:
                def __init__(self, *args, **kwargs):
                    self.args = args
                    self.kwargs = kwargs

            class Request:
                args = {}
                headers = {}
                form = {}
                files = {}
                is_json = False
                accept_mimetypes = types.SimpleNamespace(best_match=lambda values: values[0])
                @staticmethod
                def get_json(*args, **kwargs):
                    return {}

            flask.Flask = Flask
            flask.Response = Response
            flask.request = Request()
            flask.jsonify = lambda *args, **kwargs: kwargs if kwargs else args
            flask.redirect = lambda *args, **kwargs: ('redirect', args, kwargs)
            flask.send_file = lambda *args, **kwargs: ('file', args, kwargs)
            sys.modules['flask'] = flask

            import agent

            posted = {}

            class Response:
                status_code = 200
                text = 'accepted'

            class Requests:
                @staticmethod
                def post(url, json=None, timeout=None):
                    posted['url'] = url
                    posted['json'] = json
                    return Response()

            agent.requests = Requests
            agent.broker_feedback.register_plan = lambda plan: True
            agent.broker_feedback.mark_relay_result = lambda *args, **kwargs: True

            signal = {
                '_strat': 'A/B',
                '_setup_group_id': 'group-1',
                'dir': 'LONG',
                'entry': 20000.0,
                'SL': 19980.0,
                'TP': 20040.0,
                '_size_mult': 99,
                '_select_mult': 99,
            }
            plan = agent.execution_plan.attach(signal)
            result = agent._exec_order(signal, 'integration test')

            assert result['sent'] is True
            assert posted['url'] == os.environ['EXEC_WEBHOOK']
            assert posted['json']['quantity'] == plan['qty']
            assert posted['json']['quantity'] <= 8
            assert '/all/trades' in {rule.rule for rule in agent.app.url_map.iter_rules()}
            assert 'all_trades' in agent.app.view_functions
            print(json.dumps({'qty': plan['qty'], 'all_trades': True}))
            """
        )
        env = os.environ.copy()
        proc = subprocess.run(
            [sys.executable, '-c', script],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        self.assertIn('"all_trades": true', proc.stdout)


if __name__ == '__main__':
    unittest.main()
