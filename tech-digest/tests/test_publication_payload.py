"""The existing publisher must send exactly the reviewed daily payload."""
import unittest
from unittest.mock import Mock, patch

from app import publisher
from app.digest import render_reader_article
from app.publication_gate import decide_publication
from tests.reader_fixtures import article_packet, passing_review


class TestPublicationPayload(unittest.TestCase):
    def setUp(self):
        self.source, self.draft = article_packet()

    def decide(self, body):
        title = self.draft['title']
        return decide_publication(title, body, self.draft, self.source,
                                  passing_review(title, body, self.source))

    def test_overlong_closed_code_cannot_reach_publisher_truncation(self):
        self.draft['sections'][0]['markdown'] = '```python\n' + 'x=1\n' * 2300 + '```'
        body = render_reader_article(self.draft, self.source).strip()
        self.assertGreater(len(body), publisher.MAX_CONTENT_CHARS)
        self.assertEqual(self.decide(body)['status'], 'skip_quality')
        self.assertIn('content_too_long_for_publication', self.decide(body)['reasons'])

    def test_normalization_must_happen_before_review(self):
        body = render_reader_article(self.draft, self.source)
        self.assertNotEqual(body, publisher.markdown_to_plain(body))
        self.assertNotEqual(self.decide(body)['status'], 'ready')

    def test_exact_body_at_publisher_limit_is_not_changed(self):
        body = 'a' * publisher.MAX_CONTENT_CHARS
        self.assertEqual(self.decide(body)['status'], 'ready')
        self.assertEqual(publisher.markdown_to_plain(body), body)

    def test_real_publish_payload_preserves_reviewed_body_and_source_footer(self):
        body = render_reader_article(self.draft, self.source).strip()
        self.assertEqual(self.decide(body)['status'], 'ready')
        response = Mock()
        response.json.return_value = {'code': 200, 'postID': 123}
        with patch.object(publisher.settings, 'market_refresh_token', 'offline-placeholder'), \
                patch.object(publisher.settings, 'market_user_telephone', 'offline-user'), \
                patch.object(publisher, 'refresh_access_token', return_value=('offline-token', None)), \
                patch.object(publisher, '_base', return_value='https://example.org'), \
                patch.object(publisher.requests, 'post', return_value=response) as send:
            pid, _, error = publisher.publish_md(self.draft['title'], body)
        self.assertIsNone(error)
        self.assertEqual(pid, 123)
        payload = send.call_args.kwargs['json']
        self.assertEqual(payload['title'], self.draft['title'])
        self.assertEqual(payload['content'], body)
        self.assertIn(self.source['url'], payload['content'])
