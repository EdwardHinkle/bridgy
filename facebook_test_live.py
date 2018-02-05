#!/usr/bin/env python
"""Facebook integration test against the live API, using a canned user.

https://github.com/snarfed/bridgy/issues/406

The canned user is Snoopy Barrett: https://www.facebook.com/100009447618341 ,
https://brid.gy/facebook/1407574399567467 . To refresh his access token in
facebook_test_user_access_token, reauth on his bridgy user page and then grab it
from the datastore:

https://console.cloud.google.com/datastore/entities/edit?project=brid-gy&kind=FacebookAuth&key=0%2F%7C12%2FFacebookAuth%7C21%2Fname:1407574399567467

He has one post with one like and two comments:
https://www.facebook.com/100009447618341/posts/1407573252900915

  Snoopy Barrett:
    just curled up on a blanket, like you do
    (example.zz abc)
  Ryan Barrett likes this.
  Ryan Barrett: really?
  Snoopy Barrett: yup really

I'd ideally like to use a Test User, but their posts can't have comments or
likes. :(
https://developers.facebook.com/docs/apps/test-users
"""
from __future__ import unicode_literals

import logging
import sys
import unittest
import urllib
import urlparse

from requests import adapters, sessions
orig_HTTPAdapter = adapters.HTTPAdapter

import oauth_dropins.webutil.test
import appengine_config

from google.appengine.api import memcache
import mox
from oauth_dropins import facebook as oauth_facebook
import requests

import facebook
import handlers
import tasks
from test import testutil
import util

TEST_USER_ID = '1407574399567467'

# undo requests-toolbelt's app engine monkey patch
sessions.HTTPAdapter = adapters.HTTPAdapter = orig_HTTPAdapter


class FacebookTestLive(testutil.HandlerTest):

  def test_live(self):
    # sign up (use the form inputs in our actual HTML template)
    with open('templates/facebook_signup.html') as f:
      resp = self.submit_form(f.read())

    self.assertEqual(302, resp.status_int)
    to = resp.headers['Location']
    self.assertTrue(to.startswith('https://www.facebook.com/v2.10/dialog/oauth?'), to)
    params = urlparse.parse_qs(urlparse.urlparse(to).query)
    redirect = params['redirect_uri'][0]
    state = params['state'][0]
    self.dot()

    # pretend the user approves the prompt and facebook redirects back to us.
    # mock out the access token request since we use a canned token.
    self.expect_urlopen(oauth_facebook.GET_ACCESS_TOKEN_URL % {
        'client_id': appengine_config.FACEBOOK_APP_ID,
        'client_secret': appengine_config.FACEBOOK_APP_SECRET,
        'redirect_uri': urllib.quote_plus(redirect),
        'auth_code': 'fake_code',
      },
      '{"access_token": "%s"}' % appengine_config.FACEBOOK_TEST_USER_TOKEN,
    ).WithSideEffects(lambda *args, **kwargs: self.mox.stubs.UnsetAll())
    self.mox.ReplayAll()

    resp = facebook.application.get_response(
      util.add_query_params(redirect, {
        'code': 'fake_code',
        'state': urllib.unquote(state),
      }))
    self.assertEqual(302, resp.status_int)
    source = facebook.FacebookPage.get_by_id(TEST_USER_ID)
    self.assertEqual('enabled', source.status)
    self.assertEqual(['listen'], source.features)
    self.dot()

    # ignore all domains except example.zz
    util.in_webmention_blacklist = lambda domain: domain != 'example.zz'

    # poll
    self.stub_requests_head()
    resp = self.run_task(self.taskqueue_stub.GetTasks('poll')[0])
    self.assertEqual(200, resp.status_int)
    self.dot()

    # three propagates, one for the like and one for each comment
    source_urls = []

    def handle_post_body(params):
      self.assertEqual('http://example.zz/abc', params['target'])
      source_urls.append(params['source'])
      return True

    self.mox.StubOutWithMock(requests, 'post', use_mock_anything=True)
    self.expect_requests_post(
      'http://example.zz/wm', timeout=mox.IgnoreArg(), verify=mox.IgnoreArg(),
      data=mox.Func(handle_post_body), allow_redirects=False,
      headers={'Accept': '*/*'}).MultipleTimes()
    self.mox.ReplayAll()

    memcache.set('W http example.zz', 'http://example.zz/wm')
    for task in self.taskqueue_stub.GetTasks('propagate'):
      resp = self.run_task(task)
      self.assertEqual(200, resp.status_int)

    self.mox.stubs.UnsetAll()
    self.dot()

    # fetch the response handler URLs
    for url in source_urls:
      resp = handlers.application.get_response(url)
      self.assertEqual(200, resp.status_int)
      self.dot()

  @staticmethod
  def dot():
    sys.stdout.write('.')
    sys.stdout.flush()

  @staticmethod
  def submit_form(html):
    """Submits the first form on the page."""
    form = util.beautifulsoup_parse(html).form
    data = {input['name']: input['value'] for input in form.find_all('input')
            if input.get('name') and input.get('value')}
    return facebook.application.get_response(
      form['action'], method=form['method'].upper(), body=urllib.urlencode(data))

  @staticmethod
  def run_task(task):
    """Runs a task queue task."""
    return tasks.application.get_response(
      task['url'], method='POST', body=urllib.urlencode(testutil.get_task_params(task)))


if __name__ == '__main__':
  if '--debug' in sys.argv:
    sys.argv.remove('--debug')
    logging.getLogger().setLevel(logging.DEBUG)
  else:
    logging.getLogger().setLevel(logging.CRITICAL + 1)
  unittest.main()
