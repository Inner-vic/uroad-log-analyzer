import io
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cheyun_prod_client as client


class Response(io.BytesIO):
    def __init__(self, payload):
        data = json.dumps(payload).encode('utf-8')
        super().__init__(data)
        self.headers = {'Content-Length': str(len(data))}


class CloudResponseContractTests(unittest.TestCase):
    def query(self, *payloads):
        class Opener:
            def open(self, request, timeout):
                return Response(next(responses))
        responses = iter(payloads)
        with patch.object(client.urllib.request, 'build_opener', return_value=Opener()):
            return client.query_files(vin='DEM0VEH1CLEX00001', start='2026-11-02 20:57:00',
                                      end='2026-11-02 21:29:00', log_class=client.UROAD_LOG_CLASS,
                                      username='bound-user', environment='prod', page_size=1)

    def test_explicit_success_null_is_valid_empty_prod(self):
        result = self.query({'success': True, 'data': None})
        self.assertEqual((result.total, result.items, result.environment), (0, [], 'prod'))

    def test_supported_explicit_codes_confirm_empty_success(self):
        for code in (0, 200, '000000', '200'):
            with self.subTest(code=code):
                self.assertEqual(self.query({'code': code, 'data': None}).total, 0)

    def test_http_success_cannot_hide_business_failure(self):
        for payload, error in (
            ({'code': 500, 'data': None}, client.CloudServerError),
            ({'success': True, 'code': 500, 'data': None}, client.CloudServerError),
            ({'code': 403, 'data': None}, client.CloudAuthError),
            ({'success': False, 'data': None}, client.CloudResponseError),
            ({'success': 'false', 'data': None}, client.CloudResponseError),
            ({'data': None}, client.CloudResponseError),
        ):
            with self.subTest(payload=payload), self.assertRaises(error):
                self.query(payload)

    def test_malformed_empty_values_never_become_no_data(self):
        for data in ([], '', False, 0, {}, {'total': 0}, {'list': None, 'total': 0}, {'list': ''}):
            with self.subTest(data=data), self.assertRaises(client.CloudResponseError):
                self.query({'success': True, 'data': data})
        with self.assertRaises(client.CloudResponseError):
            self.query({'success': True})

    def test_empty_list_and_records_aliases_are_unambiguous(self):
        self.assertEqual(self.query({'success': True, 'data': {'list': [], 'total': 0}}).items, [])
        self.assertEqual(self.query({'success': True, 'data': {'records': [], 'total': '0'}}).items, [])
        with self.assertRaises(client.CloudResponseError):
            self.query({'success': True, 'data': {'list': [], 'records': [{'fileKey': 'x'}], 'total': 1}})

    def test_total_is_integral_nonnegative_and_never_bool(self):
        for total in (True, False, 1.5, -1, '1.5', None):
            with self.subTest(total=total), self.assertRaises(client.CloudResponseError):
                self.query({'success': True, 'data': {'list': [], 'total': total}})

    def test_valid_two_pages_keep_origin_and_order(self):
        rows = [{'fileKey': 'one', 'fileName': 'one.zst'}, {'fileKey': 'two', 'fileName': 'two.zst'}]
        result = self.query(*[{'success': True, 'data': {'list': [row], 'total': 2}} for row in rows])
        self.assertEqual(result.items, rows)
        self.assertEqual((result.total, result.provider_environment), (2, 'prod'))

    def test_success_null_on_later_page_cannot_truncate_query(self):
        with self.assertRaises(client.CloudResponseError):
            self.query({'success': True, 'data': {'list': [{'fileKey': 'one'}], 'total': 2}},
                       {'success': True, 'data': None})


if __name__ == '__main__':
    unittest.main()
