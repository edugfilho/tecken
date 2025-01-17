# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import re
import logging
import fnmatch
import zipfile
import hashlib
import os
import time
import concurrent.futures

import markus
from encore.concurrent.futures.synchronous import SynchronousExecutor

from django import http
from django.conf import settings
from django.utils import timezone
from django.core.exceptions import ImproperlyConfigured
from django.views.decorators.csrf import csrf_exempt

from tecken.base.decorators import (
    api_login_required,
    api_any_permission_required,
    api_require_POST,
    make_tempdir,
)
from tecken.base.utils import filesizeformat, invalid_key_name_characters
from tecken.upload.forms import UploadByDownloadForm, UploadByDownloadRemoteError
from tecken.upload.models import Upload, UploadsCreated
from tecken.upload.utils import (
    dump_and_extract,
    UnrecognizedArchiveFileExtension,
    DuplicateFileDifferentSize,
    upload_file_upload,
)
from tecken.librequests import session_with_retries
from tecken.storage import StorageBucket


logger = logging.getLogger("tecken")
metrics = markus.get_metrics("tecken")


class NoPossibleBucketName(Exception):
    """When you tried to specify a preferred bucket name but it never
    matched to one you can use."""


_not_hex_characters = re.compile(r"[^a-f0-9]", re.I)

# This list of filenames is used to validate a zip and also when iterating
# over the extracted zip.
# The names of files in this list are considered harmless and something that
# can simply be ignored.
_ignorable_filenames = (".DS_Store",)


def check_symbols_archive_file_listing(file_listings):
    """return a string (the error) if there was something not as expected"""
    for file_listing in file_listings:
        for snippet in settings.DISALLOWED_SYMBOLS_SNIPPETS:
            if snippet in file_listing.name:
                return (
                    f"Content of archive file contains the snippet "
                    f"'{snippet}' which is not allowed"
                )
        # Now check that the filename is matching according to these rules:
        # 1. Either /<name1>/hex/<name2>,
        # 2. Or, /<name>-symbols.txt
        # Anything else should be considered and unrecognized file pattern
        # and thus rejected.
        split = file_listing.name.split("/")
        if split[-1] in _ignorable_filenames:
            continue
        if len(split) == 3:
            # Check the symbol and the filename part of it to make sure
            # it doesn't contain any, considered, invalid S3 characters
            # when it'd become a key.
            if invalid_key_name_characters(split[0] + split[2]):
                return f"Invalid character in filename {file_listing.name!r}"
            # Check that the middle part is only hex characters.
            if not _not_hex_characters.findall(split[1]):
                continue
        elif len(split) == 1:
            if file_listing.name.lower().endswith("-symbols.txt"):
                continue

        # If it didn't get "continued" above, it's an unrecognized file
        # pattern.
        return (
            "Unrecognized file pattern. Should only be <module>/<hex>/<file> "
            "or <name>-symbols.txt and nothing else. "
            f"(First unrecognized pattern was {file_listing.name})"
        )


def get_bucket_info(user, try_symbols=None, preferred_bucket_name=None):
    """return an object that has 'bucket', 'endpoint_url',
    'region'.
    Only 'bucket' is mandatory in the response object.
    """

    if try_symbols is None:
        # If it wasn't explicitly passed, we need to figure this out by
        # looking at the user who uploads.
        # Namely, we're going to see if the user has the permission
        # 'upload.upload_symbols'. If the user does, it means the user intends
        # to *not* upload Try build symbols.
        # This is based on the axiom that, if the upload is made with an
        # API token, that API token can't have *both* the
        # 'upload.upload_symbols' permission *and* the
        # 'upload.upload_try_symbols' permission.
        # If the user uploads via the web the user has a choice to check
        # a checkbox that is off by default. If doing so, the user isn't
        # using an API token, so the user might have BOTH permissions.
        # Then the default falls on this NOT being a Try upload.
        try_symbols = not user.has_perm("upload.upload_symbols")

    if try_symbols:
        url = settings.UPLOAD_TRY_SYMBOLS_URL
    else:
        url = settings.UPLOAD_DEFAULT_URL

    exceptions = settings.UPLOAD_URL_EXCEPTIONS
    if preferred_bucket_name:
        # If the user has indicated a preferred bucket name, check that they have
        # permission to use it.
        for url, _ in get_possible_bucket_urls(user):
            if preferred_bucket_name in url:
                return StorageBucket(url, try_symbols=try_symbols)
        raise NoPossibleBucketName(preferred_bucket_name)
    else:
        if user.email.lower() in exceptions:
            # easy
            exception = exceptions[user.email.lower()]
        else:
            # match against every possible wildcard
            exception = None  # assume no match
            for email_or_wildcard in settings.UPLOAD_URL_EXCEPTIONS:
                if fnmatch.fnmatch(user.email.lower(), email_or_wildcard.lower()):
                    # a match!
                    exception = settings.UPLOAD_URL_EXCEPTIONS[email_or_wildcard]
                    break
        if exception:
            url = exception

    return StorageBucket(url, try_symbols=try_symbols)


def get_possible_bucket_urls(user):
    """Return list of possible buckets this user can upload to.

    If the user is specified in UPLOAD_URL_EXCEPTIONS, then the user can only upload
    into that bucket.

    If the user is not specified, then the user can upload to the public bucket.

    :param user: a django user

    :return: list of tuples of (url, "private"/"public")
    """
    urls = []
    exceptions = settings.UPLOAD_URL_EXCEPTIONS
    email_lower = user.email.lower()
    for email_pattern in exceptions:
        if (
            email_lower == email_pattern.lower()
            or fnmatch.fnmatch(email_lower, email_pattern.lower())
            or user.is_superuser
        ):
            urls.append((exceptions[email_pattern], "private"))

    # We use UPLOAD_URL_EXCEPTIONS to specify buckets people can upload into. If a
    # person is specified in UPLOAD_URL_EXCEPTIONS, then they can only upload to that
    # bucket. If they are not specified, then they can upload to the public bucket.
    if not urls:
        urls.append((settings.UPLOAD_DEFAULT_URL, "public"))

    return urls


def _ignore_member_file(filename):
    """Return true if the given filename (could be a filepath), should
    be completely ignored in the upload process.

    At the moment the list is "allow-list based", meaning all files are
    processed and uploaded to S3 unless it meets certain checks.
    """
    if filename.lower().endswith("-symbols.txt"):
        return True
    if os.path.basename(filename) in _ignorable_filenames:
        return True
    return False


@metrics.timer_decorator("upload_archive")
@api_require_POST
@csrf_exempt
@api_login_required
@api_any_permission_required("upload.upload_symbols", "upload.upload_try_symbols")
@make_tempdir(settings.UPLOAD_TEMPDIR_PREFIX)
def upload_archive(request, upload_dir):
    try:
        for name in request.FILES:
            upload_ = request.FILES[name]
            file_listing = dump_and_extract(upload_dir, upload_, name)
            size = upload_.size
            url = None
            redirect_urls = None
            break
        else:
            if request.POST.get("url"):
                form = UploadByDownloadForm(request.POST)
                try:
                    is_valid = form.is_valid()
                except UploadByDownloadRemoteError as exception:
                    return http.JsonResponse({"error": str(exception)}, status=500)
                if is_valid:
                    url = form.cleaned_data["url"]
                    name = form.cleaned_data["upload"]["name"]
                    size = form.cleaned_data["upload"]["size"]
                    size_fmt = filesizeformat(size)
                    logger.info(f"Download to upload {url} ({size_fmt})")
                    redirect_urls = form.cleaned_data["upload"]["redirect_urls"] or None
                    download_name = os.path.join(upload_dir, name)
                    session = session_with_retries(default_timeout=(5, 300))
                    with metrics.timer("upload_download_by_url"):
                        response_stream = session.get(url, stream=True)
                        # NOTE(willkg): The UploadByDownloadForm handles most errors
                        # when it does a HEAD, so this mostly covers transient errors
                        # between the HEAD and this GET request.
                        if response_stream.status_code != 200:
                            return http.JsonResponse(
                                {
                                    "error": "non-200 status code when retrieving %s"
                                    % url
                                },
                                status=400,
                            )

                        with open(download_name, "wb") as f:
                            # Read 1MB at a time
                            chunk_size = 1024 * 1024
                            stream = response_stream.iter_content(chunk_size=chunk_size)
                            count_chunks = 0
                            start = time.time()
                            for chunk in stream:
                                if chunk:  # filter out keep-alive new chunks
                                    f.write(chunk)
                                count_chunks += 1
                            end = time.time()
                            total_size = chunk_size * count_chunks
                            download_speed = size / (end - start)
                            logger.info(
                                f"Read {count_chunks} chunks of "
                                f"{filesizeformat(chunk_size)} each "
                                f"totalling {filesizeformat(total_size)} "
                                f"({filesizeformat(download_speed)}/s)."
                            )
                    file_listing = dump_and_extract(upload_dir, download_name, name)
                    os.remove(download_name)
                else:
                    for key, errors in form.errors.as_data().items():
                        return http.JsonResponse(
                            {"error": errors[0].message}, status=400
                        )
            else:
                return http.JsonResponse(
                    {
                        "error": (
                            "Must be multipart form data with at " "least one file"
                        )
                    },
                    status=400,
                )
    except zipfile.BadZipfile as exception:
        return http.JsonResponse({"error": str(exception)}, status=400)
    except UnrecognizedArchiveFileExtension as exception:
        return http.JsonResponse(
            {"error": f'Unrecognized archive file extension "{exception}"'}, status=400
        )
    except DuplicateFileDifferentSize as exception:
        return http.JsonResponse({"error": str(exception)}, status=400)
    error = check_symbols_archive_file_listing(file_listing)
    if error:
        return http.JsonResponse({"error": error.strip()}, status=400)

    # If you pass an extract argument, independent of value, with key 'try'
    # then we definitely knows this is a Try symbols upload.
    is_try_upload = request.POST.get("try")

    # If you have special permission, you can affect which bucket to upload to.
    preferred_bucket_name = request.POST.get("bucket_name")
    try:
        bucket_info = get_bucket_info(
            request.user,
            try_symbols=is_try_upload,
            preferred_bucket_name=preferred_bucket_name,
        )
    except NoPossibleBucketName as exception:
        logger.warning(f"No possible bucket for {request.user!r} ({exception})")
        return http.JsonResponse({"error": "No valid bucket"}, status=403)

    if is_try_upload is None:
        # If 'is_try_upload' isn't immediately true by looking at the
        # request.POST parameters, the get_bucket_info() function can
        # figure it out too.
        is_try_upload = bucket_info.try_symbols
    else:
        # In case it's passed in as a string
        is_try_upload = bool(is_try_upload)

    if not bucket_info.exists():
        raise ImproperlyConfigured(f"Bucket does not exist: {bucket_info!r}")

    # Create the client for upload_file_upload
    # TODO(jwhitlock): implement backend details in StorageBucket API
    client = bucket_info.get_storage_client(
        read_timeout=settings.S3_PUT_READ_TIMEOUT,
        connect_timeout=settings.S3_PUT_CONNECT_TIMEOUT,
    )
    # Use a different client for doing the lookups.
    # That's because we don't want the size lookup to severly accumulate
    # in the case of there being some unpredictable slowness.
    # When that happens the lookup is quickly cancelled and it assumes
    # the file does not exist.
    # See http://botocore.readthedocs.io/en/latest/reference/config.html#botocore.config.Config  # noqa
    lookup_client = bucket_info.get_storage_client(
        read_timeout=settings.S3_LOOKUP_READ_TIMEOUT,
        connect_timeout=settings.S3_LOOKUP_CONNECT_TIMEOUT,
    )

    # Every key has a prefix. If the StorageBucket instance has it's own prefix
    # prefix that first :)
    prefix = settings.SYMBOL_FILE_PREFIX
    if bucket_info.prefix:
        prefix = f"{bucket_info.prefix}/{prefix}"

    # Make a hash string that represents every file listing in the archive.
    # Do this by making a string first out of all files listed.

    content = "\n".join(
        f"{x.name}:{x.size}" for x in sorted(file_listing, key=lambda x: x.name)
    )
    # The MD5 is just used to make the temporary S3 file unique in name
    # if the client uploads with the same filename in quick succession.
    content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()[:30]  # nosec

    # Always create the Upload object no matter what happens next.
    # If all individual file uploads work out, we say this is complete.
    upload_obj = Upload.objects.create(
        user=request.user,
        filename=name,
        bucket_name=bucket_info.name,
        bucket_region=bucket_info.region,
        bucket_endpoint_url=bucket_info.endpoint_url,
        size=size,
        download_url=url,
        redirect_urls=redirect_urls,
        content_hash=content_hash,
        try_symbols=is_try_upload,
    )

    ignored_keys = []
    skipped_keys = []

    if settings.SYNCHRONOUS_UPLOAD_FILE_UPLOAD:
        # This is only applicable when running unit tests
        thread_pool = SynchronousExecutor()
    else:
        thread_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=settings.UPLOAD_FILE_UPLOAD_MAX_WORKERS or None
        )
    file_uploads_created = 0
    uploaded_symbol_keys = []
    key_to_symbol_keys = {}
    with thread_pool as executor:
        future_to_key = {}
        for member in file_listing:
            if _ignore_member_file(member.name):
                ignored_keys.append(member.name)
                continue
            key_name = os.path.join(prefix, member.name)
            # We need to know and remember, for every file attempted,
            # what that name corresponds to as a "symbol key".
            # A symbol key is, for example, ('xul.pdb', 'A7D6F1BBA7D6F1BB1')
            symbol_key = tuple(member.name.split("/")[:2])
            key_to_symbol_keys[key_name] = symbol_key
            future_to_key[
                executor.submit(
                    upload_file_upload,
                    client,
                    bucket_info.name,
                    key_name,
                    member.path,
                    upload=upload_obj,
                    client_lookup=lookup_client,
                )
            ] = key_name
        # Now lets wait for them all to finish and we'll see which ones
        # were skipped and which ones were created.
        for future in concurrent.futures.as_completed(future_to_key):
            file_upload = future.result()
            if file_upload:
                file_uploads_created += 1
                uploaded_symbol_keys.append(key_to_symbol_keys[file_upload.key])
            else:
                skipped_keys.append(future_to_key[future])
                metrics.incr("upload_file_upload_skip", 1)

    if file_uploads_created:
        logger.info(f"Created {file_uploads_created} FileUpload objects")
    else:
        logger.info(f"No file uploads created for {upload_obj!r}")

    Upload.objects.filter(id=upload_obj.id).update(
        skipped_keys=skipped_keys or None,
        ignored_keys=ignored_keys or None,
        completed_at=timezone.now(),
    )

    # Re-calculate the UploadsCreated for today.
    # FIXME(willkg): when/if we get a scheduled task runner, we should move this
    # to that
    date = timezone.now().date()
    with metrics.timer("uploads_created_update"):
        UploadsCreated.update(date)
    logger.info(f"UploadsCreated updated for {date!r}")
    metrics.incr(
        "upload_uploads", tags=[f"try:{is_try_upload}", f"bucket:{bucket_info.name}"]
    )

    return http.JsonResponse({"upload": _serialize_upload(upload_obj)}, status=201)


def _serialize_upload(upload):
    return {
        "id": upload.id,
        "size": upload.size,
        "filename": upload.filename,
        "bucket": upload.bucket_name,
        "region": upload.bucket_region,
        "download_url": upload.download_url,
        "try_symbols": upload.try_symbols,
        "redirect_urls": upload.redirect_urls or [],
        "completed_at": upload.completed_at,
        "created_at": upload.created_at,
        "user": upload.user.email,
        "skipped_keys": upload.skipped_keys or [],
    }
