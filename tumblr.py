"""Tumblr + Disqus blog webmention implementation.

http://disqus.com/api/docs/
http://disqus.com/api/docs/posts/create/
https://github.com/disqus/DISQUS-API-Recipes/blob/master/snippets/php/create-guest-comment.php
http://help.disqus.com/customer/portal/articles/466253-what-html-tags-are-allowed-within-comments-
create returns id, can lookup by id w/getContext?

guest post (w/arbitrary author, url):
http://spirytoos.blogspot.com/2013/12/not-so-easy-posting-as-guest-via-disqus.html
http://stackoverflow.com/questions/15416688/disqus-api-create-comment-as-guest
http://jonathonhill.net/2013-07-11/disqus-guest-posting-via-api/

can send url and not look up disqus thread id!
http://stackoverflow.com/questions/4549282/disqus-api-adding-comment
https://disqus.com/api/docs/forums/listThreads/

test command line:
curl localhost:8080/webmention/tumblr \
  -d 'source=http://localhost/response.html&target=http://snarfed.tumblr.com/post/60428995188/glen-canyon-http-t-co-fzc4ehiydp?foo=bar#baz'
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import collections
import datetime
import json
import logging
import os
import re
import requests
import urllib
import urlparse

import appengine_config
from appengine_config import HTTP_TIMEOUT

from activitystreams.oauth_dropins import tumblr as oauth_tumblr
import models
import requests
import util

from google.appengine.ext import ndb
import webapp2

TUMBLR_AVATAR_URL = 'http://api.tumblr.com/v2/blog/%s/avatar/512'
DISQUS_API_CREATE_POST_URL = 'https://disqus.com/api/3.0/posts/create.json'
DISQUS_API_THREAD_DETAILS_URL = 'http://disqus.com/api/3.0/threads/details.json'


class Tumblr(models.Source):
  """A Tumblr blog.

  The key name is the blog domain.
  """
  AS_CLASS = collections.namedtuple('FakeAsClass', ('NAME',))(NAME='Tumblr')
  SHORT_NAME = 'tumblr'

  disqus_shortname = ndb.StringProperty(required=True)

  def feed_url(self):
    # http://www.tumblr.com/help  (search for feed)
    return urlparse.urljoin(self.domain_url, '/rss')

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a Tumblr for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.tumblr.TumblrAuth
    """
    url, domain, ok = Tumblr._url_and_domain(auth_entity)
    if not ok:
      handler.messages = {'No primary Tumblr blog found. Please create one first!'}
      return None

    # scrape the disqus shortname out of the Tumblr page
    try:
      resp = requests.get(url, allow_redirects=True, timeout=HTTP_TIMEOUT)
      resp.raise_for_status()
    except BaseException:
      msg = 'Could not fetch %s' % domain
      logging.exception(msg)
      handler.messages = {msg}
      return None

    match = re.search('http://disqus.com/forums/([^/"\' ]+)', resp.text)
    if not match:
      handler.messages = {
        'Please <a href="http://disqus.com/admin/create/">install Disqus</a> first!'}
      return None

    return Tumblr(id=domain,
                  auth_entity=auth_entity.key,
                  domain=domain,
                  domain_url=url,
                  name=auth_entity.user_display_name(),
                  disqus_shortname=match.group(1),
                  picture=TUMBLR_AVATAR_URL % domain,
                  superfeedr_secret=util.generate_secret(),
                  **kwargs)

  @staticmethod
  def _url_and_domain(auth_entity):
    """Returns this user's primary blog URL and domain.

    Args:
      auth_entity: oauth_dropins.tumblr.TumblrAuth

    Returns: (string url, string domain, boolean ok)
    """
    # TODO: if they have multiple blogs, let them choose which one to sign up.
    #
    # user_json is the user/info response:
    # http://www.tumblr.com/docs/en/api/v2#user-methods
    for blog in json.loads(auth_entity.user_json).get('user', {}).get('blogs', []):
      if blog.get('primary'):
        return blog['url'], util.domain_from_link(blog['url']), True
    else:
      return None, None, False

  def create_comment(self, post_url, author_name, author_url, content):
    """Creates a new comment in the source silo.

    Must be implemented by subclasses.

    Args:
      post_url: string
      author_name: string
      author_url: string
      content: string

    Returns: JSON response dict with 'id' and other fields
    """
    # strip slug, query and fragment from post url
    parsed = urlparse.urlparse(post_url)
    path = parsed.path.split('/')
    try:
      tumblr_post_id = int(path[-1])
    except ValueError:
      path.pop(-1)
    post_url = urlparse.urlunparse(parsed[:2] + ('/'.join(path), '', '', ''))

    # get the disqus thread id. details on thread queries:
    # http://stackoverflow.com/questions/4549282/disqus-api-adding-comment
    # https://disqus.com/api/docs/threads/details/
    resp = self.disqus_call(requests.get, DISQUS_API_THREAD_DETAILS_URL,
                            {'forum': self.disqus_shortname,
                             # ident:[tumblr_post_id] should work, but doesn't :/
                             'thread': 'link:%s' % post_url,
                             },
                            allow_redirects=True)
    thread_id = resp['id']

    # create the comment
    message = '<a href="%s">%s</a>: %s' % (author_url, author_name, content)
    resp = self.disqus_call(requests.post, DISQUS_API_CREATE_POST_URL,
                            {'thread': thread_id,
                             'message': message,
                             # only allowed when authed as moderator/owner
                             # 'state': 'approved',
                             })
    return resp

  @staticmethod
  def disqus_call(method, url, params, **kwargs):
    """Makes a Disqus API call.

    Args:
      method: requests function to use, e.g. requests.get
      url: string
      params: query parameters
      kwargs: passed through to method

    Returns: dict, JSON response
    """
    logging.info('Calling Disqus %s with %s', url.split('/')[-2:], params)
    params.update({
        'api_key': appengine_config.DISQUS_API_KEY,
        'api_secret': appengine_config.DISQUS_API_SECRET,
        'access_token': appengine_config.DISQUS_ACCESS_TOKEN,
        })
    resp = method(url, timeout=HTTP_TIMEOUT, params=params, **kwargs)
    resp.raise_for_status()
    resp = resp.json()['response']
    logging.info('Response: %s', resp)
    return resp

class AddTumblr(oauth_tumblr.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    self.maybe_add_or_delete_source(Tumblr, auth_entity, state)


class SuperfeedrNotifyHandler(webapp2.RequestHandler):
  """Handles a Superfeedr notification.

  http://documentation.superfeedr.com/subscribers.html#pubsubhubbubnotifications
  """
  def post(self, id):
    source = Tumblr.get_by_id()
    if source and 'webmention' in source.features:
      superfeedr.handle_feed(self.request.body, source)


application = webapp2.WSGIApplication([
    ('/tumblr/start', oauth_tumblr.StartHandler.to('/tumblr/add')),
    ('/tumblr/add', AddTumblr),
    ('/tumblr/delete/start', oauth_tumblr.CallbackHandler.to('/delete/finish')),
    ('/tumblr/notify/(.+)', SuperfeedrNotifyHandler),
    ], debug=appengine_config.DEBUG)