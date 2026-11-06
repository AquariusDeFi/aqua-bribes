from django.conf import settings as django_settings
from django.core.cache import cache
from django.utils import timezone

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.status import HTTP_200_OK, HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN
from rest_framework.views import APIView

from aquarius_bribes.rewards.models import VoteSnapshot
from aquarius_bribes.rewards.tasks import LOAD_VOTES_TASK_ACTIVE_KEY, task_load_votes


class RunVotesSnapshotAPIView(APIView):
    authorization_token = django_settings.REWARD_SERVER_AUTHORIZATION_TOKEN
    permission_classes = (AllowAny, )

    def post(self, request, *args, **kwargs):
        if request.headers.get('Authorization', None) != "Bearer {}".format(self.authorization_token):
            return Response(data={'message': 'Not authorized'}, status=HTTP_401_UNAUTHORIZED)

        in_progess = cache.get(LOAD_VOTES_TASK_ACTIVE_KEY, False)
        if in_progess or VoteSnapshot.objects.filter(snapshot_time=timezone.now().date()).exists():
            return Response(data={'message': 'Snapshot already exists'}, status=HTTP_403_FORBIDDEN)

        task_load_votes.delay()
        return Response(data={'message': 'Snapshot started'}, status=HTTP_200_OK)
