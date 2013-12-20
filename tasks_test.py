"""Unit tests for tasks.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json
import logging
import mox
import urllib
import urlparse

import models
import models_test
import tasks
from tasks import Poll, Propagate
import testutil
import util
from webmentiontools import send

from google.appengine.ext import db
import webapp2

NOW = datetime.datetime.now()
tasks.now_fn = lambda: NOW


class TaskQueueTest(testutil.ModelsTest):
  """Attributes:
    task_params: the query parameters passed in the task POST request
    post_url: the URL for post_task() to post to
  """
  task_params = None
  post_url = None

  def post_task(self, expected_status=200):
    """Args:
      expected_status: integer, the expected HTTP return code
    """
    resp = tasks.application.get_response(self.post_url, method='POST',
                                          body=urllib.urlencode(self.task_params))
    self.assertEqual(expected_status, resp.status_int)


class PollTest(TaskQueueTest):

  post_url = '/_ah/queue/poll'

  def setUp(self):
    super(PollTest, self).setUp()
    self.task_params = {'source_key': self.sources[0].key(),
                        'last_polled': '1970-01-01-00-00-00'}

  def assert_responses(self):
    """Asserts that all of self.responses are saved."""
    self.assert_entities_equal(self.responses, models.Response.all())

  def test_poll(self):
    """A normal poll task."""
    self.assertEqual([], list(models.Response.all()))
    self.assertEqual([], self.taskqueue_stub.GetTasks('poll'))

    self.post_task()
    self.assert_responses()

    source = db.get(self.sources[0].key())
    self.assertEqual(NOW, source.last_polled)

    tasks = self.taskqueue_stub.GetTasks('poll')
    self.assertEqual(1, len(tasks))
    self.assertEqual('/_ah/queue/poll', tasks[0]['url'])

    params = testutil.get_task_params(tasks[0])
    self.assertEqual(str(source.key()),
                     params['source_key'])
    self.assertEqual(NOW.strftime(util.POLL_TASK_DATETIME_FORMAT),
                     params['last_polled'])

  def test_poll_error(self):
    """If anything goes wrong, the source status should be set to 'error'."""
    self.mox.StubOutWithMock(testutil.FakeSource, 'get_activities')
    testutil.FakeSource.get_activities(count=mox.IgnoreArg(), fetch_replies=True
                                       ).AndRaise(Exception('foo'))
    self.mox.ReplayAll()

    self.assertRaises(Exception, self.post_task)
    source = db.get(self.sources[0].key())
    self.assertEqual('error', source.status)

  def test_reset_status_to_enabled(self):
    """After a successful poll, the source status should be set to 'enabled'."""
    self.sources[0].status = 'error'
    self.sources[0].save()

    self.post_task()
    source = db.get(self.sources[0].key())
    self.assertEqual('enabled', source.status)

  def test_original_post_discovery(self):
    """Target URLs should be extracted from attachments, tags, and text."""
    obj = self.activities[0]['object']
    obj['tags'] = [{'objectType': 'article', 'url': 'http://tar.get/a'},
                   {'objectType': 'person', 'url': 'http://pe.rs/on'},
                   ]
    obj['attachments'] = [{'objectType': 'article', 'url': 'http://tar.get/b'}]
    obj['content'] = 'foo http://tar.get/c bar (tar.get d) baz'
    self.sources[0].set_activities([self.activities[0]])

    self.post_task()
    expected = ['http://tar.get/%s' % i for i in 'a', 'b', 'c', 'd']
    self.assert_equals(expected, db.get(self.responses[0].key()).unsent)

  def test_existing_responses(self):
    """Poll should be idempotent and not touch existing response entities.
    """
    self.responses[0].status = 'complete'
    self.responses[0].save()

    self.post_task()
    self.assert_responses()
    self.assertEqual('complete', db.get(self.responses[0].key()).status)

  def test_wrong_last_polled(self):
    """If the source doesn't have our last polled value, we should quit.
    """
    self.sources[0].last_polled = datetime.datetime.utcfromtimestamp(3)
    self.sources[0].save()
    self.post_task()
    self.assertEqual([], list(models.Response.all()))

  def test_no_source(self):
    """If the source doesn't exist, do nothing and let the task die.
    """
    self.sources[0].delete()
    self.post_task()
    self.assertEqual([], self.taskqueue_stub.GetTasks('poll'))

  def test_disable_source_on_deauthorized(self):
    """If the source raises DisableSource, disable it.
    """
    source = self.sources[0]
    self.mox.StubOutWithMock(testutil.FakeSource, 'get_activities')
    testutil.FakeSource.get_activities(count=mox.IgnoreArg(), fetch_replies=True
                                       ).AndRaise(models.DisableSource)
    self.mox.ReplayAll()

    source.status = 'enabled'
    source.save()
    self.post_task()
    source = db.get(source.key())
    self.assertEqual('disabled', source.status)


class PropagateTest(TaskQueueTest):

  post_url = '/_ah/queue/propagate'

  def setUp(self):
    super(PropagateTest, self).setUp()
    self.responses[0].save()
    self.task_params = {'response_key': self.responses[0].key()}
    self.local_url = 'http://localhost/comment/fake/%s/a/1_2_a' % \
      self.responses[0].source.key().name()
    self.mock_webmention()

  def mock_webmention(self):
    self.mock_sends = []
    for i in range(3):
      ms = self.mox.CreateMock(send.WebmentionSend)
      ms.receiver_endpoint = 'http://webmention/endpoint'
      ms.response = 'used in logging'
      self.mock_sends.append(ms)

    self.mock_send = self.mock_sends[0]
    self.mox.StubOutWithMock(send, 'WebmentionSend', use_mock_anything=True)

  def assert_response_is(self, status, leased_until=False, sent=[], error=[]):
    """Asserts that responses[0] has the given values in the datastore.
    """
    response = db.get(self.responses[0].key())
    self.assertEqual(status, response.status)
    if leased_until is not False:
      self.assertEqual(leased_until, response.leased_until)
    self.assert_equals([], response.unsent)
    self.assert_equals(sent, response.sent)
    self.assert_equals(error, response.error)

  def expect_webmention(self, target_url='http://target1/post/url'):
    send.WebmentionSend(self.local_url, target_url).InAnyOrder().AndReturn(self.mock_send)
    return self.mock_send.send(timeout=999)

  def test_propagate(self):
    """A normal propagate task."""
    self.assertEqual('new', self.responses[0].status)

    self.expect_webmention().AndReturn(True)
    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', NOW + Propagate.LEASE_LENGTH,
                           sent=['http://target1/post/url'])

  def test_propagate_from_error(self):
    """A normal propagate task, with a response starting as 'error'."""
    self.responses[0].status = 'error'
    self.responses[0].save()

    self.expect_webmention().AndReturn(True)
    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', NOW + Propagate.LEASE_LENGTH,
                           sent=['http://target1/post/url'])

  def test_multiple_targets(self):
    """We should send webmentions to the unsent and error targets."""
    self.responses[0].error = ['http://target2/x', 'http://target3/y']
    self.responses[0].sent = ['http://target4/z']
    self.responses[0].save()

    self.expect_webmention('http://target1/post/url').InAnyOrder().AndReturn(True)
    self.mock_send.error = {'code': 'RECEIVER_ERROR'}
    self.expect_webmention('http://target2/x').InAnyOrder().AndReturn(False)
    self.mock_send = self.mock_sends[1]
    self.mock_send.error = {'code': 'NO_ENDPOINT'}
    self.expect_webmention('http://target3/y').InAnyOrder().AndReturn(False)

    self.mox.ReplayAll()
    self.post_task(expected_status=Propagate.ERROR_HTTP_RETURN_CODE)
    response = db.get(self.responses[0].key())
    self.assert_response_is('error',
                           sent=['http://target1/post/url', 'http://target4/z'],
                           error=['http://target2/x'])

  def test_no_targets(self):
    """No target URLs."""
    self.responses[0].unsent = []
    self.responses[0].save()

    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', NOW + Propagate.LEASE_LENGTH)

  def test_already_complete(self):
    """If the response has already been propagated, do nothing."""
    self.responses[0].status = 'complete'
    self.responses[0].save()

    self.post_task()
    self.assert_response_is('complete')

  def test_leased(self):
    """If the response is processing and the lease hasn't expired, do nothing."""
    self.responses[0].status = 'processing'
    leased_until = NOW + datetime.timedelta(minutes=1)
    self.responses[0].leased_until = leased_until
    self.responses[0].save()

    self.post_task(expected_status=Propagate.ERROR_HTTP_RETURN_CODE)
    self.assert_response_is('processing', leased_until)

    response = db.get(self.responses[0].key())
    self.assertEqual('processing', response.status)
    self.assertEqual(leased_until, response.leased_until)

  def test_lease_expired(self):
    """If the response is processing but the lease has expired, process it."""
    self.responses[0].status = 'processing'
    self.responses[0].leased_until = NOW - datetime.timedelta(minutes=1)
    self.responses[0].save()

    self.expect_webmention().AndReturn(True)
    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', NOW + Propagate.LEASE_LENGTH,
                           sent=['http://target1/post/url'])

  def test_no_response(self):
    """If the response doesn't exist, the request should fail."""
    self.responses[0].delete()
    self.post_task(expected_status=Propagate.ERROR_HTTP_RETURN_CODE)

  def test_webmention_fail(self):
    """If sending the webmention fails, the lease should be released."""
    for code, give_up in (('NO_ENDPOINT', True),
                          ('BAD_TARGET_URL', False),
                          ('RECEIVER_ERROR', False)):
      self.mox.UnsetStubs()
      self.responses[0].status = 'new'
      self.responses[0].save()
      self.mock_webmention()
      self.expect_webmention().AndReturn(False)
      self.mock_send.error = {'code': code}
      self.mox.ReplayAll()

      logging.debug('Testing %s', code)
      expected_status = 200 if give_up else Propagate.ERROR_HTTP_RETURN_CODE
      self.post_task(expected_status=expected_status)
      if give_up:
        self.assert_response_is('complete')
      else:
        self.assert_response_is('error', error=['http://target1/post/url'])
      self.mox.VerifyAll()

  def test_webmention_fail_and_succeed(self):
    """All webmentions should be attempted, but any failure sets error status."""
    self.responses[0].unsent = ['http://first', 'http://second']
    self.responses[0].save()
    self.mock_send.error = {'code': 'FOO'}
    self.expect_webmention('http://first').AndReturn(False)
    self.expect_webmention('http://second').AndReturn(True)

    self.mox.ReplayAll()
    self.post_task(expected_status=Propagate.ERROR_HTTP_RETURN_CODE)
    self.assert_response_is('error', None, error=['http://first'],
                           sent=['http://second'])

  def test_webmention_exception(self):
    """If sending the webmention raises an exception, the lease should be released."""
    self.expect_webmention().AndRaise(Exception('foo'))
    self.mox.ReplayAll()

    self.post_task(expected_status=500)
    self.assert_response_is('error', None)

  def test_complete_exception(self):
    """If completing raises an exception, the lease should be released."""
    self.expect_webmention().AndReturn(True)
    self.mox.StubOutWithMock(Propagate, 'complete_response')
    Propagate.complete_response(mox.IgnoreArg()).AndRaise(Exception('foo'))
    self.mox.ReplayAll()

    self.post_task(expected_status=500)
    self.assert_response_is('error', None, sent=['http://target1/post/url'])
