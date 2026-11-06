from unittest import mock

from django.contrib.sites.models import Site
from django.test import SimpleTestCase

from rest_framework.test import APIClient

from aquarius_bribes.rewards.api import RunVotesSnapshotAPIView


@mock.patch.object(RunVotesSnapshotAPIView, 'authorization_token', 'snapshot-test-token')
class RunVotesSnapshotAPITests(SimpleTestCase):
    url = '/api/take-votes-snapshot/'

    def setUp(self):
        self.client = APIClient()
        self._patch('django.contrib.sites.models.Site.objects.get_current', return_value=Site(domain='testserver'))
        self.load_votes = self._patch('aquarius_bribes.rewards.api.task_load_votes.delay')
        self.cache_get = self._patch('aquarius_bribes.rewards.api.cache.get', return_value=False)
        self.snapshots = self._patch('aquarius_bribes.rewards.api.VoteSnapshot.objects.filter')
        self.snapshots.return_value.exists.return_value = False

    def _patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_options_returns_metadata_without_starting_snapshot(self):
        response = self.client.options(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertIn('POST', response['Allow'])
        self.load_votes.assert_not_called()
        self.cache_get.assert_not_called()
        self.snapshots.assert_not_called()

    def test_post_requires_authorization(self):
        for authorization in ('', 'Bearer wrong-token'):
            with self.subTest(authorization=authorization):
                response = self.client.post(self.url, HTTP_AUTHORIZATION=authorization)

                self.assertEqual(response.status_code, 401)
                self.load_votes.assert_not_called()

    def test_post_starts_snapshot(self):
        response = self.client.post(self.url, HTTP_AUTHORIZATION='Bearer snapshot-test-token')

        self.assertEqual(response.status_code, 200)
        self.load_votes.assert_called_once_with()

    def test_post_rejects_running_snapshot(self):
        self.cache_get.return_value = True

        response = self.client.post(self.url, HTTP_AUTHORIZATION='Bearer snapshot-test-token')

        self.assertEqual(response.status_code, 403)
        self.load_votes.assert_not_called()

    def test_post_rejects_existing_snapshot(self):
        self.snapshots.return_value.exists.return_value = True

        response = self.client.post(self.url, HTTP_AUTHORIZATION='Bearer snapshot-test-token')

        self.assertEqual(response.status_code, 403)
        self.load_votes.assert_not_called()

    def test_other_api_options_return_metadata(self):
        for url in ('/api/bribes/', '/api/pending-bribes/'):
            with self.subTest(url=url):
                response = self.client.options(url)

                self.assertEqual(response.status_code, 200)
