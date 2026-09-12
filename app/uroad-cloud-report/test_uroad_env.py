import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, '/workspace/skills/uroad-cloud-report/scripts')

import cheyun_prod_client as client


class CheYunEnvironmentTests(unittest.TestCase):
    def test_prod_request_params(self):
        params = client.build_query_params(
            vin='DEM0VEH1CLE000001', start='2026-09-01 23:05:00', end='2026-09-01 23:09:59',
            log_class=client.UROAD_LOG_CLASS, username='u', page_no=1, page_size=100,
        )
        self.assertEqual(params['vin'], 'DEM0VEH1CLE000001')
        self.assertEqual(params['remoteMode'], 'false')
        self.assertEqual(params['eeaPlatform'], 'EEA3.0')
        self.assertEqual(params['userName'], 'u')
        self.assertEqual(params['logClass'], 'log_fsdA_service')

    def test_testtwo_request_params(self):
        cfg = client.environment_config('test')
        self.assertEqual(cfg['providerEnvironment'], 'testtwo')
        self.assertEqual(cfg['origin'], 'https://vehicle-log-api.test.example.com')

    def test_extract_download_host(self):
        hosts = client.extract_download_hosts([{'downloadURL': 'https://foo.example.com/a?x=1'}])
        self.assertEqual(hosts, ['https://foo.example.com'])

    def test_test_download_host_whitelist(self):
        with self.assertRaises(client.CloudResponseError):
            client.validate_download_url('https://foo.example.com/a', environment='test')
        client.add_download_host_allowlist('test', 'foo.example.com', 'https')
        scheme, host = client.validate_download_url('https://foo.example.com/a?b=1', environment='test')
        self.assertEqual((scheme, host), ('https', 'foo.example.com'))

    def test_query_result_tracks_provider_environment(self):
        payloads = [
            {'success': True, 'data': {'total': 1, 'list': [{'fileName': 'AVM_Service-a.zst', 'downloadURL': 'https://foo.example.com/x'}]}}
        ]
        with mock.patch.object(client, 'request_json', side_effect=payloads):
            result = client.query_files(vin='V', start='s', end='e', log_class=client.UROAD_LOG_CLASS,
                                        username='u', environment='test')
        self.assertEqual(result.environment, 'test')
        self.assertEqual(result.provider_environment, 'testtwo')
        self.assertEqual(result.download_hosts, ['https://foo.example.com'])


if __name__ == '__main__':
    unittest.main()
