import json
import re
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
import inflect
from fastapi import WebSocket, APIRouter, WebSocketDisconnect
from server.evals import eval, hume_eval
import asyncio
import json
import uuid
from datetime import datetime
from server.db import update_call, get_call, get_all_calls
from server.socket_manager import manager
from server.geocoding import geocode, street_view
from server.prompts import SYSTEM_PROMPT

router = APIRouter(prefix="/hume")


@router.websocket("")
async def hume_endpoint(websocket: WebSocket):
    await websocket.accept()

    agent = Agent(system_prompt=SYSTEM_PROMPT)
    last_history = []
    id = str(uuid.uuid4())
    try:
        while True:
            # Wait for a text message from the WebSocket, then asynchronously receive it.
            data = await websocket.receive_text()

            # Deserialize the text message (JSON format) to a Python dictionary.
            hume_socket_message = json.loads(data)

            # Parse the received message to extract the last user message and the chat history.
            # This is necessary for understanding the context of the conversation
            message, chat_history, transcript = agent.parse_hume_message(
                hume_socket_message
            )
            last_history = chat_history

            updated_data = {
                "id": id,
                "mode": "hume",
                "time": datetime.now().isoformat(),
                "transcript": transcript + [{"role": "user", "content": message}],
            }
            update_call(id, updated_data)
            all_calls = get_all_calls()
            await manager.broadcast(
                {
                    "event": "db_response",
                    "data": all_calls,
                }
            )

            # Generate responses based on the last message and the chat history.
            responses = agent.get_responses(message, last_history)

            # Send the generated responses back to the client via the WebSocket connection.
            async for response in responses:
                await websocket.send_text(response)

            updated_data = {
                "time": datetime.now().isoformat(),
                "transcript": agent.get_transcript(),
            }
            update_call(id, updated_data)
            all_calls = get_all_calls()
            await manager.broadcast(
                {
                    "event": "db_response",
                    "data": all_calls,
                }
            )

            current_data = str(get_call(id))
            print(current_data)
            hume_task = hume_eval(message)
            eval_task = eval(message, current_data)
            results = await asyncio.gather(hume_task, eval_task)

            updated_data = {
                "emotions": results[0],
                **json.loads(results[1]),
            }

            if updated_data.get("location_name"):
                res = geocode(updated_data["location_name"])
                updated_data["location_coords"] = res[0]["geometry"]["location"]
                updated_data["street_view"] = street_view(
                    updated_data["location_coords"]["lat"],
                    updated_data["location_coords"]["lng"],
                )

            update_call(id, updated_data)
            all_calls = get_all_calls()
            await manager.broadcast(
                {
                    "event": "db_response",
                    "data": all_calls,
                }
            )

    except WebSocketDisconnect:
        print("WebSocket connection has been closed.")


class Agent:
    def __init__(self, *, system_prompt: str):
        self.system_prompt = system_prompt
        self.standard_transcript = []

        self.llm = ChatOpenAI(
            model="gpt-4o",
            temperature=0.8,
            max_tokens=1024,
            timeout=None,
            max_retries=2,
        )

    def add_prosody_to_utterance(self, utterance: str, prosody: dict) -> str:
        prosody_string = ", ".join(prosody.keys())
        return f"Speech: {utterance} {prosody_string}"

    async def get_responses(self, message: str, chat_history=None) -> list[str]:
        if chat_history is None:
            chat_history = []
        self.standard_transcript.append(
            {
                "role": "user",
                "content": message,
            }
        )
        human = HumanMessage(content=message)
        chat_history.append(human)
        text = ""

        response = self.llm.astream(chat_history)
        async for chunk in response:
            output = chunk.content
            numbers = re.findall(r"\b\d{1,3}(?:,\d{3})*(?:\.\d+)?\b", output)

            for number in numbers:
                words = self.number_to_words(number)
                output = output.replace(number, words, 1)
            text += output

            yield json.dumps({"type": "assistant_input", "text": output})
        self.standard_transcript.append(
            {
                "role": "assistant",
                "content": text,
            }
        )
        yield json.dumps({"type": "assistant_end"})

    def convert_history_to_text(self, chat_history):
        return "\n".join([message.content for message in chat_history])

    def get_transcript(self):
        return self.standard_transcript

    def parse_hume_message(self, messages_payload: dict) -> [str, list[any]]:
        messages = messages_payload["messages"]
        last_user_message = messages[-1]["message"]["content"]

        chat_history = [SystemMessage(content=self.system_prompt)]
        last_role = None
        combined_utterance = ""

        # Iterate through each message in the data except the last one
        for message in messages[:-1]:

            # Extract the message role and content
            message_object = message["message"]
            current_role = message_object["role"]

            # Extract the prosody model scores, if available
            prosody_scores = (
                message.get("models", {}).get("prosody", {}).get("scores", {})
            )

            # Sort the prosody scores based on score, in descending order
            sorted_entries = sorted(
                prosody_scores.items(), key=lambda x: x[1], reverse=True
            )

            # Extract the top 3 entries
            top_entries = sorted_entries[:3]

            # Format the top entries as a dictionary for easier readability
            top_entries_dict = {entry[0]: entry[1] for entry in top_entries}

            contextualized_utterance = self.add_prosody_to_utterance(
                message_object["content"], top_entries_dict
            )

            if current_role == "user":
                if last_role == "assistant" and combined_utterance:
                    chat_history.append(AIMessage(content=combined_utterance))
                    combined_utterance = ""
                chat_history.append(HumanMessage(content=contextualized_utterance))
            elif current_role == "assistant":
                if last_role == "assistant":
                    combined_utterance += " " + message_object["content"]
                else:
                    if combined_utterance:
                        chat_history.append(AIMessage(content=combined_utterance))
                        combined_utterance = message_object["content"]
                    else:
                        combined_utterance = message_object["content"]

            last_role = current_role

        # Append any remaining combined AI messages
        if combined_utterance:
            chat_history.append(AIMessage(content=combined_utterance))

        return [last_user_message, chat_history, self.standard_transcript]

    def generate_response(self, *, prompt: str):
        pass

    def number_to_words(self, number):
        p = inflect.engine()
        words = p.number_to_words(number)
        return words
