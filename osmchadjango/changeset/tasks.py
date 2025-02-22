# -*- coding: utf-8 -*-
from __future__ import print_function, unicode_literals
from os.path import join
from urllib.parse import quote
import yaml

from django.conf import settings
from django.utils.timezone import now

import requests
from requests_oauthlib import OAuth1Session
from celery import shared_task, group
from osmcha.changeset import Analyse, ChangesetList

from .models import Changeset, SuspicionReasons, Import


@shared_task
def create_changeset(changeset_id):
    """Analyse and create the changeset in the database."""
    ch = Analyse(changeset_id)
    ch.full_analysis()

    # remove suspicion_reasons
    ch_dict = ch.get_dict()
    ch_dict.pop('suspicion_reasons')

    # remove bbox field if it is not a valid geometry
    if ch.bbox == 'GEOMETRYCOLLECTION EMPTY':
        ch_dict.pop('bbox')

    # save changeset.
    changeset, created = Changeset.objects.update_or_create(
        id=ch_dict['id'],
        defaults=ch_dict
        )

    if ch.suspicion_reasons:
        for reason in ch.suspicion_reasons:
            reason, created = SuspicionReasons.objects.get_or_create(name=reason)
            reason.changesets.add(changeset)

    print('{c[id]} created'.format(c=ch_dict))
    return changeset


@shared_task
def get_filter_changeset_file(url, geojson_filter=settings.CHANGESETS_FILTER):
    """Filter the changesets of the replication file by the area defined in the
    GeoJSON file.
    """
    cl = ChangesetList(url, geojson_filter)
    group(create_changeset.s(c['id']) for c in cl.changesets)()


def format_url(n):
    """Return the url of a replication file."""
    return "{}/{:03d}/{:03d}/{:03d}.osm.gz".format(
        settings.OSM_CHANGESETS_URL,
        n // 1000000,
        (n // 1000) % 1000,
        (n % 1000)
    )

@shared_task
def import_replications(start, end):
    """Recieves a start and a end number and import each replication file in
    this interval.
    """
    imp, created = Import.objects.get_or_create(start=start, end=end)
    if not created:
        imp.date = now()
        imp.save()
    urls = [format_url(n) for n in range(start, end + 1)]
    group(get_filter_changeset_file.s(url) for url in urls)()


def get_last_replication_id():
    """Get the id number of the last replication file available on Planet OSM.
    """
    state = yaml.safe_load(
        requests.get(
            join(settings.OSM_CHANGESETS_URL, 'state.yaml')
        ).content
    )
    return state.get('sequence', 0)


@shared_task
def fetch_latest():
    """Function to import all the replication files since the last import or the
    last 1000.
    """
    sequence = get_last_replication_id()
    if sequence <= 0:
        print("No replication changesets to import")
        return

    last_import = Import.objects.order_by('-end').first()
    if last_import:
        start = last_import.end + 1
    else:
        if sequence <= settings.OSM_CHANGESETS_MAX_IMPORT:
            start = 1
        else:
            start = sequence - settings.OSM_CHANGESETS_MAX_IMPORT

    print("Importing replications from %d to %d" % (start, sequence,))
    import_replications(start, sequence)


class ChangesetCommentAPI(object):
    """Class that allows us to publish comments in changesets on the
    OpenStreetMap website.
    """
    def __init__(self, user, changeset_id):
        self.changeset_id = changeset_id
        user_token = user.social_auth.all().first().access_token
        self.client = OAuth1Session(
            settings.SOCIAL_AUTH_OPENSTREETMAP_KEY,
            client_secret=settings.SOCIAL_AUTH_OPENSTREETMAP_SECRET,
            resource_owner_key=user_token['oauth_token'],
            resource_owner_secret=user_token['oauth_token_secret']
            )
        self.url = f"{settings.OSM_API_URL}/api/0.6/changeset/{changeset_id}/comment/"

    def post_comment(self, message=None):
        """Post comment to changeset."""
        response = self.client.post(
            self.url,
            data='text={}'.format(quote(message)).encode("utf-8")
            )
        if response.status_code == 200:
            print(
                'Comment in the changeset {} posted successfully.'.format(
                    self.changeset_id
                    )
                )
            return {'success': True}
        else:
            print("""Some error occurred and it wasn't possible to post the
                comment to the changeset {}.""".format(self.changeset_id))
            return {'success': False}
