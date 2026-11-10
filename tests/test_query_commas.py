"""Search queries may contain commas (GitHub's `in:name,description,readme`), still bounded and URL-encoded."""

import os
import unittest
from urllib.parse import parse_qsl, urlsplit

from collector import collect, github, schema
from tests.helpers import CollectorTestCase

SPEC_QUERY = "ps3recomp in:name,description,readme fork:true"
ROOT = {"repository_id": 90, "full_name": "frame-org/ps3recomp-example"}


def config(*queries):
    return {"queries": list(queries), "roots": [ROOT], "max_requests": 50}


class QueryCommas(CollectorTestCase):
    def test_the_spec_query_form_is_accepted(self):
        loaded = collect.validate_config(config(SPEC_QUERY))
        self.assertEqual(loaded["queries"], [SPEC_QUERY])

    def test_a_comma_query_is_percent_encoded_in_the_planned_request(self):
        loaded = collect.validate_config(config(SPEC_QUERY))
        search = [r for r in collect.plan_requests(loaded) if r["path"] == "/search/repositories"]
        self.assertEqual(len(search), 1)
        self.assertNotIn(",", search[0]["query"])
        self.assertIn("%2C", search[0]["query"])
        self.assertEqual(dict(parse_qsl(search[0]["query"]))["q"], SPEC_QUERY)

    def test_the_url_the_client_would_request_has_no_raw_comma_and_passes_the_url_check(self):
        url = github.build_url("/search/repositories", {"q": SPEC_QUERY, "per_page": 100, "page": 1})
        self.assertNotIn(",", urlsplit(url).query)
        github.check_url(url)

    def test_the_length_bound_still_holds_with_commas(self):
        collect.validate_config(config("a," * (schema.MAX_QUERY // 2)))
        with self.assertRaises(schema.SchemaError):
            collect.validate_config(config("a," * (schema.MAX_QUERY // 2 + 1)))

    def test_other_separators_and_urls_stay_refused(self):
        for bad in ("a&per_page=1", "a=b", "a?b", "a%2Cb", "a;b", "a\nb", "a\tb", "a,b\x00", "http://x.example,y", "a\\b", "a{b}", "a<b>"):
            with self.subTest(query=bad), self.assertRaises(schema.SchemaError):
                collect.validate_config(config(bad))

    def test_duplicate_queries_are_still_refused(self):
        with self.assertRaises(schema.SchemaError):
            collect.validate_config(config(SPEC_QUERY, SPEC_QUERY))


class DataIsIgnored(unittest.TestCase):
    def test_data_directory_is_in_gitignore(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, ".gitignore"), encoding="utf-8") as handle:
            lines = [line.strip() for line in handle]
        self.assertIn("data/", lines)


if __name__ == "__main__":
    unittest.main()
