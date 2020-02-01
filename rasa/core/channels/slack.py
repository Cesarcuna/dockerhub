import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Text

from rasa.core.channels.channel import InputChannel, OutputChannel, UserMessage
from rasa.utils.common import raise_warning
from sanic import Blueprint, response
from sanic.request import Request
from sanic.response import HTTPResponse
from slackclient import SlackClient

logger = logging.getLogger(__name__)


class SlackBot(SlackClient, OutputChannel):
    """A Slack communication channel"""

    @classmethod
    def name(cls) -> Text:
        return "slack"

    def __init__(self, token: Text, slack_channel: Optional[Text] = None) -> None:

        self.slack_channel = slack_channel
        super().__init__(token)

    @staticmethod
    def _get_text_from_slack_buttons(buttons: List[Dict]) -> Text:
        return "".join([b.get("title", "") for b in buttons])

    async def send_text_message(
        self, recipient_id: Text, text: Text, **kwargs: Any
    ) -> None:
        recipient = self.slack_channel or recipient_id
        for message_part in text.split("\n\n"):
            super().api_call(
                "chat.postMessage",
                channel=recipient,
                as_user=True,
                text=message_part,
                type="mrkdwn",
            )

    async def send_image_url(
        self, recipient_id: Text, image: Text, **kwargs: Any
    ) -> None:
        recipient = self.slack_channel or recipient_id
        image_block = {"type": "image", "image_url": image, "alt_text": image}
        return super().api_call(
            "chat.postMessage",
            channel=recipient,
            as_user=True,
            text=image,
            blocks=[image_block],
        )

    async def send_attachment(
        self, recipient_id: Text, attachment: Dict[Text, Any], **kwargs: Any
    ) -> None:
        recipient = self.slack_channel or recipient_id
        return super().api_call(
            "chat.postMessage",
            channel=recipient,
            as_user=True,
            attachments=[attachment],
            **kwargs,
        )

    async def send_text_with_buttons(
        self,
        recipient_id: Text,
        text: Text,
        buttons: List[Dict[Text, Any]],
        **kwargs: Any,
    ) -> None:
        recipient = self.slack_channel or recipient_id

        text_block = {"type": "section", "text": {"type": "plain_text", "text": text}}

        if len(buttons) > 5:
            raise_warning(
                "Slack API currently allows only up to 5 buttons. "
                "Since you added more than 5, slack will ignore all of them."
            )
            return await self.send_text_message(recipient, text, **kwargs)

        button_block = {"type": "actions", "elements": []}
        for button in buttons:
            button_block["elements"].append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": button["title"]},
                    "value": button["payload"],
                }
            )
        super().api_call(
            "chat.postMessage",
            channel=recipient,
            as_user=True,
            text=text,
            blocks=[text_block, button_block],
        )

    async def send_custom_json(
        self, recipient_id: Text, json_message: Dict[Text, Any], **kwargs: Any
    ) -> None:
        json_message.setdefault("channel", self.slack_channel or recipient_id)
        json_message.setdefault("as_user", True)
        return super().api_call("chat.postMessage", **json_message)


class SlackInput(InputChannel):
    """Slack input channel implementation. Based on the HTTPInputChannel."""

    @classmethod
    def name(cls) -> Text:
        return "slack"

    @classmethod
    def from_credentials(cls, credentials: Optional[Dict[Text, Any]]) -> InputChannel:
        if not credentials:
            cls.raise_missing_credentials_exception()

        # pytype: disable=attribute-error
        return cls(
            credentials.get("slack_token"),
            credentials.get("slack_channel"),
            credentials.get("slack_retry_reason_header", "x-slack-retry-reason"),
            credentials.get("slack_retry_number_header", "x-slack-retry-num"),
            credentials.get("errors_ignore_retry", None),
        )
        # pytype: enable=attribute-error

    def __init__(
        self,
        slack_token: Text,
        slack_channel: Optional[Text] = None,
        slack_retry_reason_header: Optional[Text] = None,
        slack_retry_number_header: Optional[Text] = None,
        errors_ignore_retry: Optional[List[Text]] = None,
    ) -> None:
        """Create a Slack input channel.

        Needs a couple of settings to properly authenticate and validate
        messages. Details to setup:

        https://github.com/slackapi/python-slackclient

        Args:
            slack_token: Your Slack Authentication token. You can create a
                Slack app and get your Bot User OAuth Access Token
                `here <https://api.slack.com/slack-apps>`_.
            slack_channel: the string identifier for a channel to which
                the bot posts, or channel name (e.g. '#bot-test')
                If not set, messages will be sent back
                to the "App" DM channel of your bot's name.
            slack_retry_reason_header: Slack HTTP header name indicating reason that slack send retry request.
            slack_retry_number_header: Slack HTTP header name indicating the attempt number
            errors_ignore_retry: Any error codes given by Slack
                included in this list will be ignored.
                Error codes are listed
                `here <https://api.slack.com/events-api#errors>`_.

        """
        self.slack_token = slack_token
        self.slack_channel = slack_channel
        self.errors_ignore_retry = errors_ignore_retry or ("http_timeout",)
        self.retry_reason_header = slack_retry_reason_header
        self.retry_num_header = slack_retry_number_header

    @staticmethod
    def _is_user_message(slack_event: Dict) -> bool:
        return (
            slack_event.get("event")
            and (
                slack_event.get("event", {}).get("type") == "message"
                or slack_event.get("event", {}).get("type") == "app_mention"
            )
            and slack_event.get("event", {}).get("text")
            and not slack_event.get("event", {}).get("bot_id")
        )

    @staticmethod
    def _sanitize_user_message(text, uids_to_remove) -> Text:
        """Remove superfluous/wrong/problematic tokens from a message.

        Probably a good starting point for pre-formatting of user-provided text
        to make NLU's life easier in case they go funky to the power of extreme

        In the current state will just drop self-mentions of bot itself

        Args:
            text: raw message as sent from slack
            uids_to_remove: a list of user ids to remove from the content

        Returns:
            str: parsed and cleaned version of the input text
        """

        for uid_to_remove in uids_to_remove:
            # heuristic to format majority cases OK
            # can be adjusted to taste later if needed,
            # but is a good first approximation
            for regex, replacement in [
                (fr"<@{uid_to_remove}>\s", ""),
                (fr"\s<@{uid_to_remove}>", ""),  # a bit arbitrary but probably OK
                (fr"<@{uid_to_remove}>", " "),
            ]:
                text = re.sub(regex, replacement, text)

        """Find mailto or http links like <mailto:xyz@rasa.com|xyz@rasa.com> or '<http://url.com|url.com>in text and substitute it with original content
        """

        pattern = r"\<(mailto:|(http|https):\/\/).*\|.*\>"
        match = re.search(pattern, text)

        if match:
            regex = match.group(0)
            replacement = regex.split("|")[1]
            replacement = replacement.replace(">", "")
            text = text.replace(regex, replacement)
        return text.strip()

    @staticmethod
    def _is_interactive_message(payload: Dict) -> bool:
        """Check wheter the input is a supported interactive input type."""

        supported = [
            "button",
            "select",
            "static_select",
            "external_select",
            "conversations_select",
            "users_select",
            "channels_select",
            "overflow",
            "datepicker",
        ]
        if payload.get("actions"):
            action_type = payload["actions"][0].get("type")
            if action_type in supported:
                return True
            elif action_type:
                logger.warning(
                    "Received input from a Slack interactive component of type "
                    f"'{payload['actions'][0]['type']}', for which payload parsing is not yet supported."
                )
        return False

    @staticmethod
    def _get_interactive_response(action: Dict) -> Optional[Text]:
        """Parse the payload for the response value."""

        if action["type"] == "button":
            return action.get("value")
        elif action["type"] == "select":
            return action.get("selected_options", [{}])[0].get("value")
        elif action["type"] == "static_select":
            return action.get("selected_option", {}).get("value")
        elif action["type"] == "external_select":
            return action.get("selected_option", {}).get("value")
        elif action["type"] == "conversations_select":
            return action.get("selected_conversation")
        elif action["type"] == "users_select":
            return action.get("selected_user")
        elif action["type"] == "channels_select":
            return action.get("selected_channel")
        elif action["type"] == "overflow":
            return action.get("selected_option", {}).get("value")
        elif action["type"] == "datepicker":
            return action.get("selected_date")

    async def process_message(
        self,
        request: Request,
        on_new_message: Callable[[UserMessage], Awaitable[Any]],
        text,
        sender_id: Optional[Text],
        metadata: Optional[Dict],
    ) -> Any:
        """Slack retries to post messages up to 3 times based on
        failure conditions defined here:
        https://api.slack.com/events-api#failure_conditions
        """
        retry_reason = request.headers.get(self.retry_reason_header)
        retry_count = request.headers.get(self.retry_num_header)
        if retry_count and retry_reason in self.errors_ignore_retry:
            logger.warning(
                f"Received retry #{retry_count} request from slack"
                f" due to {retry_reason}."
            )

            return response.text(None, status=201, headers={"X-Slack-No-Retry": 1})

        try:
            out_channel = self.get_output_channel()
            user_msg = UserMessage(
                text,
                out_channel,
                sender_id,
                input_channel=self.name(),
                metadata=metadata,
            )

            await on_new_message(user_msg)
        except Exception as e:
            logger.error(f"Exception when trying to handle message.{e}")
            logger.error(str(e), exc_info=True)

        return response.text("")

    def blueprint(
        self, on_new_message: Callable[[UserMessage], Awaitable[Any]]
    ) -> Blueprint:
        slack_webhook = Blueprint("slack_webhook", __name__)

        @slack_webhook.route("/", methods=["GET"])
        async def health(_: Request) -> HTTPResponse:
            return response.json({"status": "ok"})

        @slack_webhook.route("/webhook", methods=["GET", "POST"])
        async def webhook(request: Request) -> HTTPResponse:
            if request.form:
                output = request.form
                payload = json.loads(output["payload"][0])

                if self._is_interactive_message(payload):
                    sender_id = payload["user"]["id"]
                    text = self._get_interactive_response(payload["actions"][0])
                    if text is not None:
                        metadata = self.get_metadata(request)
                        return await self.process_message(
                            request, on_new_message, text, sender_id, metadata
                        )
                    elif payload["actions"][0]["type"] == "button":
                        # link buttons don't have "value", don't send their clicks to bot
                        return response.text("User clicked link button")
                return response.text(
                    "The input message could not be processed.", status=500
                )

            elif request.json:
                output = request.json
                if "challenge" in output:
                    return response.json(output.get("challenge"))

                elif self._is_user_message(output):
                    metadata = self.get_metadata(request)
                    return await self.process_message(
                        request,
                        on_new_message,
                        self._sanitize_user_message(
                            output["event"]["text"], output["authed_users"]
                        ),
                        output.get("event").get("user"),
                        metadata,
                    )

            return response.text("Bot message delivered")

        return slack_webhook

    def get_output_channel(self) -> OutputChannel:
        return SlackBot(self.slack_token, self.slack_channel)
