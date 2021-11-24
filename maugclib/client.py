"""Abstract class for writing chat clients."""

from typing import Tuple, Optional, IO, Dict, Union
import asyncio
import base64
import binascii
import collections
import datetime
import json
import logging
import mimetypes
import os
import random
import cgi

from yarl import URL

from google.protobuf import message as proto

from . import googlechat_pb2, exceptions, channel, http_utils, event, parsers

logger = logging.getLogger(__name__)
IMAGE_UPLOAD_URL = 'https://chat.google.com/uploads'
# Timeout to send for setactiveclient requests:
ACTIVE_TIMEOUT_SECS = 120
# Minimum timeout between subsequent setactiveclient requests:
SETACTIVECLIENT_LIMIT_SECS = 60
# API key for `key` parameter (from Hangouts web client)
API_KEY = 'AIzaSyD7InnYR3VKdb4j2rMUEbTCIr2VyEazl6k'
# Base URL for API requests:
GC_BASE_URL = 'https://chat.google.com'
BASE_URL = 'https://chat-pa.clients6.google.com'


class Client:
    """Instant messaging client for Google Chat.

    Maintains a connections to the servers, emits events, and accepts commands.

    Args:
        token_manager: (auth.TokenManager): The token manager.
        max_retries (int): (optional) Maximum number of connection attempts
            hangups will make before giving up. Defaults to 5.
        retry_backoff_base (int): (optional) The base term for the exponential
            backoff. The following equation is used when calculating the number
            of seconds to wait prior to each retry:
            retry_backoff_base^(# of retries attempted thus far)
            Defaults to 2.
    """

    def __init__(self, token_manager, max_retries=5, retry_backoff_base=2):
        self._max_retries = max_retries
        self._retry_backoff_base = retry_backoff_base

        self.on_connect = event.Event('Client.on_connect')
        """
        :class:`.Event` fired when the client connects for the first time.
        """

        self.on_reconnect = event.Event('Client.on_reconnect')
        """
        :class:`.Event` fired when the client reconnects after being
        disconnected.
        """

        self.on_disconnect = event.Event('Client.on_disconnect')
        """
        :class:`.Event` fired when the client is disconnected.
        """

        self.on_stream_event = event.Event('Client.on_stream_event')
        """
        :class:`.Event` fired when an update arrives from the server.

        Args:
            state_update: A ``StateUpdate`` message.
        """

        # http_utils.Session instance (populated by .connect()):
        self._session = None

        # The token manager that renews our tokens for us.
        self._token_manager = token_manager

        # channel.Channel instance (populated by .connect()):
        self._channel = None

        # Future for Channel.listen (populated by .connect()):
        self._listen_future = None

        self._gc_request_header = googlechat_pb2.RequestHeader(
            client_type=googlechat_pb2.RequestHeader.ClientType.IOS,
            client_version=2440378181258,
            client_feature_capabilities=googlechat_pb2.ClientFeatureCapabilities(
                spam_room_invites_level=googlechat_pb2.ClientFeatureCapabilities.CapabilityLevel.FULLY_SUPPORTED,
            )
        )

        # String identifying this client (populated later):
        self._client_id = None

        # String email address for this account (populated later):
        self._email = None

        # Active client management parameters:
        # Time in seconds that the client as last set as active:
        self._last_active_secs = 0.0
        # ActiveClientState enum int value or None:
        self._active_client_state = None

    ##########################################################################
    # Public methods
    ##########################################################################

    async def connect(self) -> None:
        """Establish a connection to the chat server.

        Returns when an error has occurred, or :func:`disconnect` has been
        called.
        """
        proxy = os.environ.get('HTTP_PROXY')
        self._session = http_utils.Session(self._token_manager, proxy=proxy)
        try:
            self._channel = channel.Channel(
                self._session, self._max_retries, self._retry_backoff_base
            )

            # Forward the Channel events to the Client events.
            self._channel.on_connect.add_observer(self.on_connect.fire)
            self._channel.on_reconnect.add_observer(self.on_reconnect.fire)
            self._channel.on_disconnect.add_observer(self.on_disconnect.fire)
            self._channel.on_receive_array.add_observer(self._on_receive_array)

            # Wrap the coroutine in a Future so it can be cancelled.
            self._listen_future = asyncio.ensure_future(self._channel.listen())
            # Listen for StateUpdate messages from the Channel until it
            # disconnects.
            try:
                await self._listen_future
            except asyncio.CancelledError:
                # If this task is cancelled, we need to cancel our child task
                # as well. We don't need an additional yield because listen
                # cancels immediately.
                self._listen_future.cancel()
            logger.info(
                'Client.connect returning because Channel.listen returned'
            )
        finally:
            await self._session.close()

    async def disconnect(self) -> None:
        """Gracefully disconnect from the server.

        When disconnection is complete, :func:`connect` will return.
        """
        logger.info('Graceful disconnect requested')
        # Cancel the listen task. We don't need an additional yield because
        # listen cancels immediately.
        self._listen_future.cancel()

    def get_gc_request_header(self) -> googlechat_pb2.RequestHeader:
        return self._gc_request_header

    @staticmethod
    def get_client_generated_id() -> int:
        """Return ``client_generated_id`` for use when constructing requests.

        Returns:
            Client generated ID.
        """
        return random.randint(0, 2 ** 32)

    async def download_attachment(self, url: Union[str, URL], max_size: int
                                  ) -> Tuple[bytes, str, str]:
        """
        Download an attachment that was present in a chat message.

        Args:
            url: The URL from :prop:`ChatMessageEvent.attachments`
            max_size: The maximum size to download. If this is greater than zero and
                the Content-Length response header is greater than this value, then the
                attachment will not be downloaded and a :class:`FileTooLargeError` will
                be raised instead.

        Returns:
            A tuple containing the raw data, the mime type (from Content-Type)
            and the file name (from Content-Disposition).
        """
        if isinstance(url, str):
            url = URL(url)
        async with self._session.fetch_raw_ctx("GET", url) as resp:
            resp.raise_for_status()
            try:
                _, params = cgi.parse_header(resp.headers["Content-Disposition"])
                filename = params.get("filename") or url.path.split("/")[-1]
            except KeyError:
                filename = url.path.split("/")[-1]
            mime = resp.headers["Content-Type"]
            if 0 < max_size < int(resp.headers["Content-Length"]):
                raise exceptions.FileTooLargeError("Image size larger than maximum")
            data = await resp.read()
            return data, mime, filename

    async def upload_image(self, image_file: IO, group_id: str, *,
                           filename: Optional[str] = None) -> googlechat_pb2.UploadMetadata:
        """Upload an image that can be later attached to a chat message.

        Args:
            image_file: A file-like object containing an image.
            group_id: (str): The group id that this image is being uploaded for.
            filename (str): (optional) Custom name for the uploaded file.

        Raises:
            hangups.NetworkError: If the upload request failed.

        Returns:
            :class:`googlechat_pb2.UploadMetadata` instance.
        """
        image_filename = filename or os.path.basename(image_file.name)
        image_data = image_file.read()

        mime_type, _ = mimetypes.guess_type(image_filename)
        if mime_type is None:
            mime_type = 'application/octet-stream'

        headers = {
            'x-goog-upload-protocol': 'resumable',
            'x-goog-upload-command': 'start',
            'x-goog-upload-content-length': f'{len(image_data)}',
            'x-goog-upload-content-type': mime_type,
            'x-goog-upload-file-name': image_filename,
        }

        params = {
            'group_id': group_id,
        }

        # request an upload URL
        res = await self._base_request(IMAGE_UPLOAD_URL, None, '', None,
                                       headers, params)

        try:
            upload_url = res.headers['x-goog-upload-url']
        except KeyError:
            raise exceptions.NetworkError(
                'image upload failed: can not acquire an upload url'
            )

        # upload the image to the upload URL
        headers = {
            'x-goog-upload-command': 'upload, finalize',
            'x-goog-upload-protocol': 'resumable',
            'x-goog-upload-offset': '0',
        }

        res = await self._base_request(upload_url, None, '', image_data, headers=headers,
                                       method='PUT')

        try:
            upload_metadata = googlechat_pb2.UploadMetadata()
            upload_metadata.ParseFromString(base64.b64decode(res.body))
        except binascii.Error as e:
            raise exceptions.NetworkError(
                'Failed to decode base64 response: {}'.format(e)
            )
        except proto.DecodeError as e:
            raise exceptions.NetworkError(
                'Failed to decode Protocol Buffer response: {}'.format(e)
            )

        return upload_metadata

    async def update_read_timestamp(self, conversation_id: str, read_timestamp: datetime.datetime
                                    ) -> None:
        try:
            await self.proto_mark_group_read_state(
                googlechat_pb2.MarkGroupReadstateRequest(
                    request_header=self.get_gc_request_header(),
                    id=parsers.group_id_from_id(conversation_id),
                    last_read_time=parsers.to_timestamp(read_timestamp),
                )
            )
        except exceptions.NetworkError as e:
            logger.warning('Failed to update read timestamp: {}'.format(e))
            raise

    async def react(self, conversation_id: str, thread_id: str, message_id: str, emoji: str,
                    remove: bool = False) -> None:
        await self.proto_update_reaction(googlechat_pb2.UpdateReactionRequest(
            request_header=self.get_gc_request_header(),
            emoji=googlechat_pb2.Emoji(unicode=emoji),
            message_id=googlechat_pb2.MessageId(
                parent_id=googlechat_pb2.MessageParentId(topic_id=googlechat_pb2.TopicId(
                    group_id=parsers.group_id_from_id(conversation_id),
                    topic_id=thread_id or message_id,
                )),
                message_id=message_id or thread_id,
            ),
            type=(googlechat_pb2.UpdateReactionRequest.REMOVE if remove
                  else googlechat_pb2.UpdateReactionRequest.ADD),
        ))

    async def delete_message(self, conversation_id: str, thread_id: str, message_id: str
                     ) -> googlechat_pb2.DeleteMessageResponse:
        return await self.proto_delete_message(googlechat_pb2.DeleteMessageRequest(
            request_header=self.get_gc_request_header(),
            message_id=googlechat_pb2.MessageId(
                parent_id=googlechat_pb2.MessageParentId(topic_id=googlechat_pb2.TopicId(
                    group_id=parsers.group_id_from_id(conversation_id),
                    topic_id=thread_id or message_id,
                )),
                message_id=message_id or thread_id,
            ),
        ))

    async def edit_message(self, conversation_id: str, thread_id: str, message_id: str,
                           text: str) -> googlechat_pb2.EditMessageResponse:
        return await self.proto_edit_message(googlechat_pb2.EditMessageRequest(
            request_header=self.get_gc_request_header(),
            message_id=googlechat_pb2.MessageId(
                parent_id=googlechat_pb2.MessageParentId(topic_id=googlechat_pb2.TopicId(
                    group_id=parsers.group_id_from_id(conversation_id),
                    topic_id=thread_id or message_id,
                )),
                message_id=message_id or thread_id,
            ),
            text_body=text,
        ))

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        image_id: Optional[googlechat_pb2.UploadMetadata] = None,
        thread_id: Optional[str] = None,
        local_id: Optional[str] = None,
    ) -> Union[googlechat_pb2.CreateTopicResponse, googlechat_pb2.CreateMessageResponse]:
        """Send a message to this conversation.

        A per-conversation lock is acquired to ensure that messages are sent in
        the correct order when this method is called multiple times
        asynchronously.

        Args:
            conversation_id: The conversation ID to send the message to.
            text: :str: The contents of the message to send.
            image_id: (optional) The image metadata from :meth:`upload_image`.
            thread_id: (optional) ID of the first message in the thread to reply to
            local_id: (optional) local transaction ID to identify message echo

        Raises:
            .NetworkError: If the message cannot be sent.

        Returns:
            :class:`.ConversationEvent` representing the new message.
        """
        annotations = None

        if image_id:
            annotations = [
                googlechat_pb2.Annotation(
                    type=googlechat_pb2.AnnotationType.UPLOAD_METADATA,
                    upload_metadata=image_id,
                    chip_render_type=googlechat_pb2.Annotation.ChipRenderType.RENDER,
                )
            ]

        try:
            local_id = local_id or f'hangups%{random.randint(0, 0xffffffffffffffff)}'
            if thread_id:
                request = googlechat_pb2.CreateMessageRequest(
                    request_header=self.get_gc_request_header(),
                    parent_id=googlechat_pb2.MessageParentId(
                        topic_id=googlechat_pb2.TopicId(
                            group_id=parsers.group_id_from_id(conversation_id),
                            topic_id=thread_id,
                        ),
                    ),
                    local_id=local_id,
                    text_body=text,
                    annotations=annotations,
                )
                return await self.proto_create_message(request)
            else:
                request = googlechat_pb2.CreateTopicRequest(
                    request_header=self.get_gc_request_header(),
                    group_id=parsers.group_id_from_id(conversation_id),
                    local_id=local_id,
                    text_body=text,
                    history_v2=True,
                    annotations=annotations,
                )
                return await self.proto_create_topic(request)
        except exceptions.NetworkError as e:
            logger.warning('Failed to send message: {}'.format(e))
            raise

    ##########################################################################
    # Private methods
    ##########################################################################

    @staticmethod
    def _get_upload_session_status(res):
        """Parse the image upload response to obtain status.

        Args:
            res: http_utils.FetchResponse instance, the upload response

        Returns:
            dict, sessionStatus of the response

        Raises:
            hangups.NetworkError: If the upload request failed.
        """
        response = json.loads(res.body.decode())
        if 'sessionStatus' not in response:
            try:
                info = (
                    response['errorMessage']['additionalInfo']
                    ['uploader_service.GoogleRupioAdditionalInfo']
                    ['completionInfo']['customerSpecificInfo']
                )
                reason = '{} : {}'.format(info['status'], info['message'])
            except KeyError:
                reason = 'unknown reason'
            raise exceptions.NetworkError('image upload failed: {}'.format(
                reason
            ))
        return response['sessionStatus']

    async def _on_receive_array(self, array):
        """Parse channel array and call the appropriate events."""
        if array[0] == 'noop':
            pass  # This is just a keep-alive, ignore it.
        else:
            if 'data' in array[0]:
                data = array[0]['data']

                resp = googlechat_pb2.StreamEventsResponse()
                resp.ParseFromString(base64.b64decode(data))

                # An event can have multiple bodies embedded in it. However,
                # instead of pushing all bodies in the same place, there first
                # one is a separate field. So to simplify handling, we muck
                # around with the class by swapping the embedded bodies into
                # the top level body field and fire the event like it was the
                # toplevel body.

                embedded_bodies = resp.event.bodies
                if len(embedded_bodies) > 0:
                    resp.event.ClearField('bodies')

                await self.on_stream_event.fire(resp.event)

                for body in embedded_bodies:
                    resp_copy = googlechat_pb2.StreamEventsResponse()
                    resp_copy.CopyFrom(resp)
                    resp_copy.event.body.CopyFrom(body)
                    resp_copy.event.type = body.event_type
                    await self.on_stream_event.fire(resp_copy.event)

    async def _gc_request(self, endpoint, request_pb: proto.Message, response_pb: proto.Message
                          ) -> None:
        """Send a Protocol Buffer formatted chat API request.

        Args:
            endpoint (str): The chat API endpoint to use.
            request_pb: The request body as a Protocol Buffer message.
            response_pb: The response body as a Protocol Buffer message.

        Raises:
            NetworkError: If the request fails.
        """
        logger.debug('Sending Protocol Buffer request %s:\n%s', endpoint,
                     request_pb)
        res = await self._base_request(
            '{}/api/{}?rt=b'.format(GC_BASE_URL, endpoint),
            'application/x-protobuf',  # Request body is Protocol Buffer.
            'proto',  # Response body is Protocol Buffer.
            request_pb.SerializeToString()
        )
        try:
            response_pb.ParseFromString(res.body)
        except proto.DecodeError as e:
            raise exceptions.NetworkError(
                'Failed to decode Protocol Buffer response: {}'.format(e)
            )
        # logger.debug('Received Protocol Buffer response:\n%s', response_pb)

    async def _base_request(
        self,
        url: str,
        content_type: Optional[str],
        response_type: str,
        data: Optional[str],
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, str]] = None,
        method: str = 'POST'
    ):
        """Send a generic authenticated POST request.

        Args:
            url (str): URL of request.
            content_type (str): Request content type.
            response_type (str): The desired response format. Valid options
                are: 'json' (JSON), 'protojson' (pblite), and 'proto' (binary
                Protocol Buffer). 'proto' requires manually setting an extra
                header 'X-Goog-Encode-Response-If-Executable: base64'.
            data (str): Request body data.

        Returns:
            FetchResponse: Response containing HTTP code, cookies, and body.

        Raises:
            NetworkError: If the request fails.
        """
        if headers is None:
            headers = {}

        if content_type is not None:
            headers['content-type'] = content_type

        if response_type == 'proto':
            # This header is required for Protocol Buffer responses. It causes
            # them to be base64 encoded:
            headers['X-Goog-Encode-Response-If-Executable'] = 'base64'

        if params is None:
            params = {}

        params.update({
            # "alternative representation type" (desired response format).
            'alt': response_type,
            # API key (required to avoid 403 Forbidden "Daily Limit for
            # Unauthenticated Use Exceeded. Continued use requires signup").
            'key': API_KEY,
        })
        res = await self._session.fetch(
            method, url, headers=headers, params=params, data=data,
        )
        return res

    ###########################################################################
    # API request methods - wrappers for self._pb_request for calling
    # particular APIs.
    ###########################################################################

    async def proto_get_user_presence(
        self, get_user_presence_request: googlechat_pb2.GetUserPresenceRequest,
    ) -> googlechat_pb2.GetUserPresenceResponse:
        """Return one or more user presences."""

        response = googlechat_pb2.GetUserPresenceResponse()
        await self._gc_request('get_user_presence',
                               get_user_presence_request, response)
        return response

    async def proto_get_members(
        self, get_members_request: googlechat_pb2.GetMembersRequest,
    ) -> googlechat_pb2.GetMembersResponse:
        """Return one or more members"""

        response = googlechat_pb2.GetMembersResponse()
        await self._gc_request('get_members', get_members_request, response)
        return response

    async def proto_paginated_world(
        self, paginate_world_request: googlechat_pb2.PaginatedWorldRequest,
    ) -> googlechat_pb2.PaginatedWorldResponse:
        """Gets a list of all conversations"""
        response = googlechat_pb2.PaginatedWorldResponse()

        await self._gc_request('paginated_world', paginate_world_request,
                               response)

        return response

    async def proto_get_self_user_status(
        self, get_self_user_status_request: googlechat_pb2.GetSelfUserStatusRequest,
    ) -> googlechat_pb2.GetSelfUserStatusResponse:
        """Return info about the current user.

           Replace get_self_info.
        """
        response = googlechat_pb2.GetSelfUserStatusResponse()
        await self._gc_request('get_self_user_status', get_self_user_status_request,
                               response)
        return response

    async def proto_get_group(
        self, get_group_request: googlechat_pb2.GetGroupRequest,
    ) -> googlechat_pb2.GetGroupResponse:
        """Looks up a group chat"""
        response = googlechat_pb2.GetGroupResponse()
        await self._gc_request('get_group', get_group_request, response)
        return response

    async def proto_mark_group_read_state(
        self, mark_group_read_state_request: googlechat_pb2.MarkGroupReadstateRequest,
    ) -> googlechat_pb2.MarkGroupReadstateResponse:
        """Marks the group's read state."""
        response = googlechat_pb2.MarkGroupReadstateResponse()
        await self._gc_request('mark_group_readstate',
                               mark_group_read_state_request,
                               response)
        return response

    async def proto_create_topic(
        self, create_topic_request: googlechat_pb2.CreateTopicRequest,
    ) -> googlechat_pb2.CreateTopicResponse:
        """Creates a top (sends a message)"""
        response = googlechat_pb2.CreateTopicResponse()
        await self._gc_request('create_topic', create_topic_request, response)
        return response

    async def proto_create_message(
        self, create_message_request: googlechat_pb2.CreateMessageRequest,
    ) -> googlechat_pb2.CreateMessageResponse:
        """Creates a message which is a response to a thread"""
        response = googlechat_pb2.CreateMessageResponse()
        await self._gc_request('create_message', create_message_request,
                               response)
        return response

    async def proto_update_reaction(
        self, update_reaction_request: googlechat_pb2.UpdateReactionRequest,
    ) -> googlechat_pb2.UpdateReactionResponse:
        """Reacts to a message"""
        response = googlechat_pb2.UpdateReactionResponse()
        await self._gc_request('update_reaction', update_reaction_request, response)
        return response

    async def proto_delete_message(
        self, delete_message_request: googlechat_pb2.DeleteMessageRequest,
    ) -> googlechat_pb2.DeleteMessageResponse:
        """Reacts to a message"""
        response = googlechat_pb2.DeleteMessageResponse()
        await self._gc_request('delete_message', delete_message_request, response)
        return response

    async def proto_edit_message(
        self, edit_message_request: googlechat_pb2.EditMessageRequest,
    ) -> googlechat_pb2.EditMessageResponse:
        """Reacts to a message"""
        response = googlechat_pb2.EditMessageResponse()
        await self._gc_request('edit_message', edit_message_request, response)
        return response


UploadedImage = collections.namedtuple('UploadedImage', ['image_id', 'url'])
"""Details about an uploaded image.

Args:
    image_id (str): Image ID of uploaded image.
    url (str): URL of uploaded image.
"""