import logging
import re
import time
from openai.error import Timeout
from slack_bolt import App, Ack, BoltContext, BoltResponse
from slack_bolt.request.payload_utils import is_event
from slack_sdk.web import WebClient
from app.utils import log, feedback
import os

from app.env import (
    SYSTEM_TEXT,
    TRANSLATE_MARKDOWN,
    OPENAI_MODEL,
    OPENAI_TEMPERATURE
)
# from app.i18n import translate
from app.openai_ops import (
    start_receiving_openai_response,
    format_openai_message_content,
    consume_openai_stream_to_write_reply,
    build_system_text,
    messages_within_context_window,
)
from app.slack_ops import (
    find_parent_message,
    is_no_mention_thread,
    post_wip_message,
    update_wip_message,
)

from app.utils import redact_string

#
# Listener functions
#

queue=0
incr=0 

def just_ack(ack: Ack):
    ack()


DEFAULT_LOADING_TEXT = ":hourglass_flowing_sand: Wait a second, please ..."


def respond_to_app_mention(
    context: BoltContext,
    payload: dict,
    client: WebClient,
    logger: logging.Logger,
):
    
    if payload.get("thread_ts") is not None:
        parent_message = find_parent_message(
            client, context.channel_id, payload.get("thread_ts")
        )
        if parent_message is not None:
            if is_no_mention_thread(context, parent_message):
                # The message event handler will reply to this
                return

    wip_reply = None

    # Replace placeholder for Slack user ID in the system prompt
    system_text = build_system_text(SYSTEM_TEXT, TRANSLATE_MARKDOWN, context)
    messages = [{"role": "system", "content": system_text}]
    try:

        user_id = context.actor_user_id or context.user_id

        if payload.get("thread_ts") is not None:
            # Mentioning the bot user in a thread
            replies_in_thread = client.conversations_replies(
                channel=context.channel_id,
                ts=payload.get("thread_ts"),
                include_all_metadata=True,
                limit=1000,
            ).get("messages", [])
            for reply in replies_in_thread:
                reply_text = redact_string(reply.get("text"))
                messages.append(
                    {
                        "role": (
                            "assistant"
                            if reply["user"] == context.bot_user_id
                            else "user"
                        ),
                        "content": (
                            f"<@{reply['user']}>: "
                            + format_openai_message_content(
                                reply_text, TRANSLATE_MARKDOWN
                            )
                        ),
                    }
                )
        else:
            # Strip bot Slack user ID from initial message
            msg_text = re.sub(f"<@{context.bot_user_id}>\\s*", "", payload["text"])
            msg_text = redact_string(msg_text)
            messages.append(
                {
                    "role": "user",
                    "content": f"<@{user_id}>: "
                    + format_openai_message_content(msg_text, TRANSLATE_MARKDOWN),
                }
            )
        
        # loading_text = translate(
        #      context=context, text=DEFAULT_LOADING_TEXT
        # )
        loading_text=DEFAULT_LOADING_TEXT
        wip_reply = post_wip_message(
            client=client,
            channel=context.channel_id,
            thread_ts=payload["ts"],
            loading_text=loading_text,
            messages=messages,
            user=context.user_id,
        )

        (
            messages,
            num_context_tokens,
            max_context_tokens,
        ) = messages_within_context_window(messages, model=OPENAI_MODEL)
        num_messages = len([msg for msg in messages if msg.get("role") != "system"])
        if num_messages == 0:
            update_wip_message(
                client=client,
                channel=context.channel_id,
                ts=wip_reply["message"]["ts"],
                text=f":warning: The previous message is too long ({num_context_tokens}/{max_context_tokens} prompt tokens).",
                messages=messages,
                user=context.user_id,
            )
        else:
            global queue
            global incr
            currentqueue=queue
            queue+=1
            while(currentqueue!=incr):
                pass
            start=time.time()
            stream = start_receiving_openai_response(
               
                temperature=OPENAI_TEMPERATURE,
                messages=messages,
                
            )
            incr+=1
            consume_openai_stream_to_write_reply(
                client=client,
                wip_reply=wip_reply,
                context=context,
                user_id=user_id,
                messages=messages,
                stream=stream,
                translate_markdown=TRANSLATE_MARKDOWN,
            )
            
            prompt=messages[len(messages)-2]["content"]
            response=messages[len(messages)-1]["content"]
    except Timeout:
        if wip_reply is not None:
            text = (
                (
                    wip_reply.get("message", {}).get("text", "")
                    if wip_reply is not None
                    else ""
                )
                + "\n\n"
                
            )
            client.chat_update(
                channel=context.channel_id,
                ts=wip_reply["message"]["ts"],
                text=text,
            )
            prompt=messages[len(messages)-1]["content"]
            response=text
    except Exception as e:
        text = (
            (
                wip_reply.get("message", {}).get("text", "")
                if wip_reply is not None
                else ""
            )
            + "\n\n"
            + f":warning: An error occurred: {e}"
        )
        logger.exception(text, e)
        if wip_reply is not None:
            client.chat_update(
                channel=context.channel_id,
                ts=wip_reply["message"]["ts"],
                text=text,
            )
        prompt=messages[len(messages)-1]["content"]
        response=text
    
    end=time.time()
    
    log(ts=payload["ts"],thread=payload["ts"],channel=context.channel_id,user=payload["user"],prompt=prompt,response=response, start=start,end=end)

def respond_to_new_message(
    context: BoltContext,
    payload: dict,
    client: WebClient,
    logger: logging.Logger,
):
    
    if payload.get("bot_id") is not None and payload.get("bot_id") != context.bot_id:
        # Skip a new message by a different app
        return

    wip_reply = None
    
    try:
        is_in_dm_with_bot = payload.get("channel_type") == "im"
        is_no_mention_required = False
        thread_ts = payload.get("thread_ts")
        if is_in_dm_with_bot is False and thread_ts is None:
            return

       

        messages_in_context = []
        if is_in_dm_with_bot is True and thread_ts is None:
            # In the DM with the bot
            past_messages = client.conversations_history(
                channel=context.channel_id,
                include_all_metadata=True,
                limit=100,
            ).get("messages", [])
            past_messages.reverse()
            # Remove old messages
            for message in past_messages:
                seconds = time.time() - float(message.get("ts"))
                if seconds < 86400:  # less than 1 day
                    messages_in_context.append(message)
            is_no_mention_required = True
        else:
            # In a thread with the bot in a channel
            messages_in_context = client.conversations_replies(
                channel=context.channel_id,
                ts=thread_ts,
                include_all_metadata=True,
                limit=1000,
            ).get("messages", [])
            if is_in_dm_with_bot is True:
                is_no_mention_required = True
            else:
                the_parent_message_found = False
                for message in messages_in_context:
                    if message.get("ts") == thread_ts:
                        the_parent_message_found = True
                        is_no_mention_required = is_no_mention_thread(context, message)
                        break
                if the_parent_message_found is False:
                    parent_message = find_parent_message(
                        client, context.channel_id, thread_ts
                    )
                    if parent_message is not None:
                        is_no_mention_required = is_no_mention_thread(
                            context, parent_message
                        )

        messages = []
        user_id = context.actor_user_id or context.user_id
        last_assistant_idx = -1
        indices_to_remove = []
        for idx, reply in enumerate(messages_in_context):
            maybe_event_type = reply.get("metadata", {}).get("event_type")
            if maybe_event_type == "gpt4all-convo":
                if context.bot_id != reply.get("bot_id"):
                    # Remove messages by a different app
                    indices_to_remove.append(idx)
                    continue
                maybe_new_messages = (
                    reply.get("metadata", {}).get("event_payload", {}).get("messages")
                )
                if maybe_new_messages is not None:
                    if len(messages) == 0 or user_id is None:
                        new_user_id = (
                            reply.get("metadata", {})
                            .get("event_payload", {})
                            .get("user")
                        )
                        if new_user_id is not None:
                            user_id = new_user_id
                    messages = maybe_new_messages
                    last_assistant_idx = idx

        if is_no_mention_required is False:
            return

        if is_in_dm_with_bot is True or last_assistant_idx == -1:
            # To know whether this app needs to start a new convo
            if not next(filter(lambda msg: msg["role"] == "system", messages), None):
                # Replace placeholder for Slack user ID in the system prompt
                system_text = build_system_text(
                    SYSTEM_TEXT, TRANSLATE_MARKDOWN, context
                )
                messages.insert(0, {"role": "system", "content": system_text})

        filtered_messages_in_context = []
        for idx, reply in enumerate(messages_in_context):
            # Strip bot Slack user ID from initial message
            if idx == 0:
                reply["text"] = re.sub(
                    f"<@{context.bot_user_id}>\\s*", "", reply["text"]
                )
            if idx not in indices_to_remove:
                filtered_messages_in_context.append(reply)
        if len(filtered_messages_in_context) == 0:
            return

        for reply in filtered_messages_in_context:
            msg_user_id = reply.get("user")
            reply_text = redact_string(reply.get("text"))
            messages.append(
                {
                    "content": f"<@{msg_user_id}>: "
                    + format_openai_message_content(reply_text, TRANSLATE_MARKDOWN),
                    "role": "user",
                }
            )

        loading_text = DEFAULT_LOADING_TEXT
        wip_reply = post_wip_message(
            client=client,
            channel=context.channel_id,
            thread_ts=payload.get("thread_ts") if is_in_dm_with_bot else payload["ts"],
            loading_text=loading_text,
            messages=messages,
            user=user_id,
        )

        (
            messages,
            num_context_tokens,
            max_context_tokens,
        ) = messages_within_context_window(messages, model=OPENAI_MODEL)
        num_messages = len([msg for msg in messages if msg.get("role") != "system"])
        if num_messages == 0:
            update_wip_message(
                client=client,
                channel=context.channel_id,
                ts=wip_reply["message"]["ts"],
                text=f":warning: The previous message is too long ({num_context_tokens}/{max_context_tokens} prompt tokens).",
                messages=messages,
                user=context.user_id,
            )
        else:

            global queue
            global incr
            currentqueue=queue
            queue+=1
            while(currentqueue!=incr):
                pass
            start=time.time()
            stream = start_receiving_openai_response(
               
                temperature=OPENAI_TEMPERATURE,
                messages=messages,
               
            )
            incr+=1
            latest_replies = client.conversations_replies(
                channel=context.channel_id,
                ts=wip_reply.get("ts"),
                include_all_metadata=True,
                limit=1000,
            )
            if (
                latest_replies.get("messages", [])[-1]["ts"]
                != wip_reply["message"]["ts"]
            ):
                # Since a new reply will come soon, this app abandons this reply
                client.chat_delete(
                    channel=context.channel_id,
                    ts=wip_reply["message"]["ts"],
                )
                return

            consume_openai_stream_to_write_reply(
                client=client,
                wip_reply=wip_reply,
                context=context,
                user_id=user_id,
                messages=messages,
                stream=stream,
                translate_markdown=TRANSLATE_MARKDOWN,
            )
            
            prompt=messages[len(messages)-2]["content"]
            response=messages[len(messages)-1]["content"]
    except Exception as e:
        text = (
            (
                wip_reply.get("message", {}).get("text", "")
                if wip_reply is not None
                else ""
            )
            + "\n\n"
            + f":warning: Failed to reply: {e}"
        )
        logger.exception(text, e)
        if wip_reply is not None:
            client.chat_update(
                channel=context.channel_id,
                ts=wip_reply["message"]["ts"],
                text=text,
            )
        incr+=1
        prompt=messages[len(messages)-2]["content"]
        response=text
    if(thread_ts is not None):
        realThread=thread_ts
    else:
        realThread=context.channel_id
    
    end=time.time()
    log(ts=payload["ts"],thread=realThread,channel=context.channel_id,user=payload["user"],prompt=prompt,response=response,start=start,end=end)


def react_feedback(  
    context: BoltContext,
    payload: dict,
    client: WebClient,
    logger: logging.Logger,
    ):
    result = client.conversations_replies(
        channel=payload.get("item").get("channel"),
        inclusive=True,
        ts=payload.get("item").get("ts")
    )
    message = result["messages"][0]
    
    if(client.auth_test().get("user_id")==payload.get("item_user") and DEFAULT_LOADING_TEXT!=message["text"]):
        if(payload.get("item").get("channel")[0]=="D"):
            # In DMs
            final = client.conversations_history(
                channel=payload.get("item").get("channel"),
                inclusive=True,
                latest=payload.get("item").get("ts"),
                limit=2
            )
            feedback(
                ts=final["messages"][-1]["ts"],
                mood=payload.get("reaction"),
                channel=payload.get("item").get("channel"),
                added=payload['type']=="reaction_added"
            )
        else:
            # In thread
            parent_ts=result["messages"][0]["thread_ts"]
            parent = client.conversations_history(
                channel=payload.get("item").get("channel"),
                inclusive=True,
                latest=parent_ts,
                limit=1
            )
            final = client.conversations_replies(
                channel=payload.get("item").get("channel"),
                inclusive=True,
                ts=parent["messages"][0]["thread_ts"],
                latest=payload.get("item").get("ts"),
                limit=2
            )
            feedback(
                ts=final["messages"][-2]["ts"],
                mood=payload.get("reaction"),
                channel=payload.get("item").get("channel"),
                added=payload['type']=="reaction_added"
            )
       
    

def register_listeners(app: App):
    app.event("app_mention")(ack=just_ack, lazy=[respond_to_app_mention])
    app.event("message")(ack=just_ack, lazy=[respond_to_new_message])
    app.event("reaction_added")(ack=just_ack, lazy=[react_feedback])
    app.event("reaction_removed")(ack=just_ack, lazy=[react_feedback])
    

MESSAGE_SUBTYPES_TO_SKIP = ["message_changed", "message_deleted"]


# To reduce unnecessary workload in this app,
# this before_authorize function skips message changed/deleted events.
# Especially, "message_changed" events can be triggered many times when the app rapidly updates its reply.
def before_authorize(
    body: dict,
    payload: dict,
    logger: logging.Logger,
    next_,
):
    if (
        is_event(body)
        and payload.get("type") == "message"
        and payload.get("subtype") in MESSAGE_SUBTYPES_TO_SKIP
    ):
        logger.debug(
            "Skipped the following middleware and listeners "
            f"for this message event (subtype: {payload.get('subtype')})"
        )
        return BoltResponse(status=200, body="")
    next_()
