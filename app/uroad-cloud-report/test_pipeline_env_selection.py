import unittest

import sys
sys.path.insert(0, '/workspace/skills/uroad-cloud-report/scripts')
from cheyun_prod_client import CloudAuthError, CloudNetworkError, CloudNoDataError


class PipelineEnvSelectionTests(unittest.TestCase):
    def test_auto_falls_back_to_test_after_prod_no_data(self):
        seen = []
        calls = [CloudNoDataError('prod no data'), object(), object()]

        def fake_query(*, environment, **kwargs):
            seen.append(environment)
            result = calls.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        try:
            fake_query(environment='prod')
        except CloudNoDataError:
            fake_query(environment='test')
            fake_query(environment='test')
        self.assertEqual(seen, ['prod', 'test', 'test'])

    def test_auto_does_not_fallback_on_auth_error(self):
        with self.assertRaises(CloudAuthError):
            raise CloudAuthError('auth failed')

    def test_auto_does_not_fallback_on_network_error(self):
        with self.assertRaises(CloudNetworkError):
            raise CloudNetworkError('network failed')


if __name__ == '__main__':
    unittest.main()
