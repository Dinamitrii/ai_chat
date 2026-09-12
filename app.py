from flask import (
    Flask,
    request,
    jsonify,
    render_template_string,
    session,
)

from openai import OpenAI

import base64
import math
import os
import uuid
import sqlite3
import requests

from threading import Lock

# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    "local-ai-assistant-secret-key-change-me"
)

app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

# ============================================================
# PATHS
# ============================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

DB_PATH = os.path.join(
    BASE_DIR,
    "chat_memory.db"
)

# ============================================================
# LLAMA.CPP
# ============================================================

ai_client = OpenAI(
    base_url="http://127.0.0.1:8080/v1",
    api_key="local-llama"
)

SYSTEM_PROMPT = """
Ти си локален AI асистент.

Отговаряй по подразбиране на български език.

Използвай информацията от предишните съобщения в текущия
разговор.

Ако потребителят вече е дал информация за себе си,
компютъра си, проектите си или текущата задача, използвай
тази информация когато е релевантна.

Не твърди, че помниш нещо, ако то не присъства в историята.

Бъди полезен, точен и ясен.

Не измисляй факти.
""".strip()

# ============================================================
# CONTEXT
# ============================================================

# llama-server също трябва да е стартиран с:
#
# --ctx-size 32768
#
MAX_CONTEXT_TOKENS = 32768

MAX_RESPONSE_TOKENS = 2048

MAX_HISTORY_TOKENS = (
        MAX_CONTEXT_TOKENS
        - MAX_RESPONSE_TOKENS
)

# ============================================================
# LOCKS
# ============================================================

chat_lock = Lock()

sd_lock = Lock()

# ============================================================
# SD SERVER
# ============================================================

SD_URL = "http://127.0.0.1:8081"


# ============================================================
# SQLITE
# ============================================================

def get_db_connection():
    connection = sqlite3.connect(
        DB_PATH,
        timeout=30
    )

    connection.row_factory = sqlite3.Row

    return connection


def init_database():
    connection = get_db_connection()

    try:

        connection.execute("""
                           CREATE TABLE IF NOT EXISTS conversations
                           (
                               id
                               TEXT
                               PRIMARY
                               KEY,
                               created_at
                               DATETIME
                               DEFAULT
                               CURRENT_TIMESTAMP,
                               updated_at
                               DATETIME
                               DEFAULT
                               CURRENT_TIMESTAMP
                           )
                           """)

        connection.execute("""
                           CREATE TABLE IF NOT EXISTS messages
                           (
                               id
                               INTEGER
                               PRIMARY
                               KEY
                               AUTOINCREMENT,

                               conversation_id
                               TEXT
                               NOT
                               NULL,

                               role
                               TEXT
                               NOT
                               NULL,

                               content
                               TEXT
                               NOT
                               NULL,

                               created_at
                               DATETIME
                               DEFAULT
                               CURRENT_TIMESTAMP,

                               FOREIGN
                               KEY
                           (
                               conversation_id
                           )
                               REFERENCES conversations
                           (
                               id
                           )
                               ON DELETE CASCADE
                               )
                           """)

        connection.execute("""
                           CREATE INDEX IF NOT EXISTS
                               idx_messages_conversation
                               ON messages(conversation_id, id)
                           """)

        connection.commit()

    finally:

        connection.close()


# Създаваме DB и таблиците при старт.
init_database()


# ============================================================
# SESSION / CONVERSATION
# ============================================================

def get_chat_id():
    chat_id = session.get(
        "chat_id"
    )

    if not chat_id:
        chat_id = str(
            uuid.uuid4()
        )

        session["chat_id"] = chat_id

    ensure_conversation_exists(
        chat_id
    )

    return chat_id


def ensure_conversation_exists(
        chat_id
):
    connection = get_db_connection()

    try:

        connection.execute(
            """
            INSERT
            OR IGNORE INTO conversations (
                id
            )
            VALUES (?)
            """,
            (
                chat_id,
            )
        )

        connection.commit()

    finally:

        connection.close()


# ============================================================
# DATABASE CHAT FUNCTIONS
# ============================================================

def add_message(
        chat_id,
        role,
        content
):
    connection = get_db_connection()

    try:

        connection.execute(
            """
            INSERT INTO messages (conversation_id,
                                  role,
                                  content)
            VALUES (?, ?, ?)
            """,
            (
                chat_id,
                role,
                content
            )
        )

        connection.execute(
            """
            UPDATE conversations
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                chat_id,
            )
        )

        connection.commit()

    finally:

        connection.close()


def get_messages(
        chat_id
):
    connection = get_db_connection()

    try:

        rows = connection.execute(
            """
            SELECT id,
                   role,
                   content,
                   created_at
            FROM messages
            WHERE conversation_id = ?
            ORDER BY id ASC
            """,
            (
                chat_id,
            )
        ).fetchall()

        return [
            {
                "id":
                    row["id"],

                "role":
                    row["role"],

                "content":
                    row["content"],

                "created_at":
                    row["created_at"]
            }
            for row
            in rows
        ]

    finally:

        connection.close()


def clear_messages(
        chat_id
):
    connection = get_db_connection()

    try:

        connection.execute(
            """
            DELETE
            FROM messages
            WHERE conversation_id = ?
            """,
            (
                chat_id,
            )
        )

        connection.execute(
            """
            UPDATE conversations
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                chat_id,
            )
        )

        connection.commit()

    finally:

        connection.close()


def delete_message_by_id(
        message_id
):
    connection = get_db_connection()

    try:

        connection.execute(
            """
            DELETE
            FROM messages
            WHERE id = ?
            """,
            (
                message_id,
            )
        )

        connection.commit()

    finally:

        connection.close()


# ============================================================
# TOKEN ESTIMATION
# ============================================================

def estimate_tokens(
        text
):
    if not text:
        return 0

    return max(
        1,
        math.ceil(
            len(text) / 4
        )
    )


def estimate_message_tokens(
        message
):
    return (
            estimate_tokens(
                message.get(
                    "role",
                    ""
                )
            )
            +
            estimate_tokens(
                message.get(
                    "content",
                    ""
                )
            )
            +
            8
    )


def estimate_history_tokens(
        history
):
    return sum(
        estimate_message_tokens(
            message
        )
        for message
        in history
    )


# ============================================================
# BUILD MODEL CONTEXT
# ============================================================

def build_context(
        chat_id
):
    db_messages = get_messages(
        chat_id
    )

    history = [
        {
            "role":
                "system",

            "content":
                SYSTEM_PROMPT
        }
    ]

    for message in db_messages:

        if message["role"] not in (
                "user",
                "assistant"
        ):
            continue

        history.append(
            {
                "role":
                    message["role"],

                "content":
                    message["content"]
            }
        )

    # ========================================================
    # Подрязване за контекста
    #
    # ВАЖНО:
    # Това НЕ трие старите съобщения от SQLite.
    #
    # Просто не ги праща към модела,
    # ако вече няма място в 32K.
    #
    # Така:
    #
    # SQLite = пълна история
    #
    # llama.cpp context = последната релевантна част
    # ========================================================

    while (
            estimate_history_tokens(
                history
            )
            >
            MAX_HISTORY_TOKENS
            and
            len(history) > 3
    ):

        history.pop(1)

        if (
                len(history) > 2
                and
                history[1]["role"]
                ==
                "assistant"
        ):
            history.pop(1)

    return history


# ============================================================
# MAIN PAGE
# ============================================================

@app.route(
    "/",
    methods=["GET"]
)
def home_page():
    get_chat_id()

    html_content = """
<!DOCTYPE html>

<html lang="bg">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>
Локален AI Асистент
</title>


<style>

* {
    box-sizing: border-box;
}


body {

    font-family:
        'Segoe UI',
        Arial,
        sans-serif;

    background:
        #f0f2f5;

    margin: 0;

    padding: 24px;

    min-height: 100vh;
}


.workspace {

    width: 1440px;

    max-width: 100%;

    margin:
        0 auto;

    display: grid;

    grid-template-columns:
        minmax(0, 1fr)
        420px;

    gap: 24px;

    align-items: start;
}


/* ==========================================================
   HEADER
   ========================================================== */

.chat-header {

    background:
        #007bff;

    color:
        white;

    padding:
        16px 20px;

    font-weight:
        bold;

    font-size:
        18px;

    display:
        flex;

    align-items:
        center;

    gap:
        10px;

    min-height:
        62px;
}


/* ==========================================================
   SD PANEL
   ========================================================== */

.sd-panel {

    background:
        white;

    border:
        1px solid #e0e0e0;

    border-radius:
        16px;

    overflow:
        hidden;

    box-shadow:
        0 8px 24px
        rgba(0,0,0,.08);

    min-width:
        0;
}


.sd-body {

    padding:
        24px;
}


.sd-panel label {

    display:
        flex;

    flex-direction:
        column;

    gap:
        7px;

    font-size:
        14px;

    color:
        #394454;

    margin-bottom:
        16px;
}


.sd-panel input,
.sd-panel textarea {

    width:
        100%;

    min-width:
        0;

    border:
        1px solid #ccd0d5;

    border-radius:
        8px;

    padding:
        10px;

    font:
        inherit;

    outline:
        none;
}


.sd-panel textarea {

    resize:
        vertical;
}


.sd-grid {

    display:
        grid;

    grid-template-columns:
        repeat(
            3,
            minmax(0,1fr)
        );

    gap:
        12px;
}


.sd-button {

    background:
        #007bff;

    color:
        white;

    border:
        none;

    min-height:
        44px;

    border-radius:
        8px;

    padding:
        0 18px;

    cursor:
        pointer;

    font-size:
        15px;
}


.sd-button:hover {

    background:
        #0056b3;
}


button:disabled {

    opacity:
        .55;

    cursor:
        wait;
}


.sd-preview {

    background:
        #f8f9fa;

    border:
        1px dashed #ccd0d5;

    border-radius:
        12px;

    padding:
        16px;

    margin-top:
        16px;

    text-align:
        center;
}


.sd-preview img {

    max-width:
        100%;

    height:
        auto;

    border-radius:
        8px;

    margin-bottom:
        8px;
}


#init-preview {

    max-height:
        160px;

    max-width:
        100%;

    margin-bottom:
        12px;
}


#sd-status {

    white-space:
        pre-wrap;

    overflow-wrap:
        anywhere;

    color:
        #465166;
}


/* ==========================================================
   CHAT
   ========================================================== */

.chat-container {

    width:
        100%;

    height:
        min(
            800px,
            90vh
        );

    background:
        white;

    border-radius:
        16px;

    box-shadow:
        0 8px 24px
        rgba(0,0,0,.1);

    display:
        flex;

    flex-direction:
        column;

    overflow:
        hidden;

    border:
        1px solid #e0e0e0;

    position:
        sticky;

    top:
        24px;
}


.chat-title {

    display:
        flex;

    align-items:
        center;

    gap:
        10px;

    flex:
        1;
}


.online-dot {

    width:
        10px;

    height:
        10px;

    background:
        #2ecc71;

    border-radius:
        50%;
}


.new-chat-button {

    border:
        1px solid
        rgba(255,255,255,.55);

    background:
        rgba(255,255,255,.12);

    color:
        white;

    border-radius:
        8px;

    padding:
        8px 10px;

    cursor:
        pointer;

    font-size:
        13px;
}


.new-chat-button:hover {

    background:
        rgba(255,255,255,.22);
}


.chat-messages {

    flex:
        1;

    padding:
        20px;

    overflow-y:
        auto;

    background:
        #f8f9fa;

    display:
        flex;

    flex-direction:
        column;

    gap:
        15px;
}


.message {

    max-width:
        88%;

    padding:
        12px 16px;

    border-radius:
        14px;

    font-size:
        15px;

    line-height:
        1.45;

    overflow-wrap:
        anywhere;

    white-space:
        pre-wrap;
}


.bot {

    align-self:
        flex-start;

    background:
        white;

    color:
        #333;

    border:
        1px solid #e4e6eb;

    border-top-left-radius:
        4px;
}


.user {

    align-self:
        flex-end;

    background:
        #007bff;

    color:
        white;

    border-top-right-radius:
        4px;
}


.typing {

    align-self:
        flex-start;

    background:
        transparent;

    color:
        #777;

    font-style:
        italic;

    display:
        none;

    font-size:
        14px;
}


.chat-input-area {

    padding:
        15px;

    background:
        white;

    border-top:
        1px solid #eee;

    display:
        flex;

    gap:
        10px;
}


.chat-input-area input {

    flex:
        1;

    min-width:
        0;

    padding:
        12px 18px;

    border:
        1px solid #ccd0d5;

    border-radius:
        24px;

    outline:
        none;

    font-size:
        15px;
}


.send-button {

    background:
        #007bff;

    color:
        white;

    border:
        none;

    width:
        45px;

    height:
        45px;

    flex:
        0 0 45px;

    border-radius:
        50%;

    cursor:
        pointer;

    display:
        flex;

    align-items:
        center;

    justify-content:
        center;

    font-size:
        18px;
}


.send-button:hover {

    background:
        #0056b3;
}


/* ==========================================================
   MEMORY INFO
   ========================================================== */

.memory-info {

    padding:
        6px 15px;

    border-top:
        1px solid #eee;

    background:
        #fafafa;

    color:
        #777;

    font-size:
        11px;

    text-align:
        center;
}


/* ==========================================================
   RESPONSIVE
   ========================================================== */

@media (
    max-width: 950px
) {

    .workspace {

        grid-template-columns:
            1fr;
    }


    .chat-container {

        position:
            static;

        height:
            650px;
    }


    body {

        padding:
            12px;
    }
}


@media (
    max-width: 480px
) {

    .sd-grid {

        grid-template-columns:
            1fr 1fr;
    }


    .sd-body {

        padding:
            16px;
    }
}

</style>

</head>


<body>


<main class="workspace">


<!-- ========================================================
     IMAGE GENERATOR
     ======================================================== -->

<section class="sd-panel">


<div class="chat-header">

sd-server · Изображения

</div>


<div class="sd-body">


<form id="sd-form">


<label>

Prompt

<textarea
    id="sd-prompt"
    rows="4"
    required
    placeholder="Опишете изображението..."
></textarea>

</label>


<label>

Negative prompt

<textarea
    id="sd-negative"
    rows="2"
    placeholder="Нежелани елементи..."
></textarea>

</label>


<div class="sd-grid">


<label>

Размер

<input
    value="512 × 512"
    readonly
>

</label>


<label>

Steps

<input
    id="sd-steps"
    type="number"
    min="1"
    max="100"
    value="20"
    required
>

</label>


<label>

CFG

<input
    id="sd-cfg"
    type="number"
    min="0"
    max="30"
    step="0.1"
    value="3.5"
    required
>

</label>


<label>

Seed

<input
    id="sd-seed"
    type="number"
    min="-1"
    max="2147483647"
    value="-1"
    required
>

</label>


<label>

Batch

<input
    value="1"
    readonly
>

</label>


<label>

Strength

<input
    id="sd-strength"
    type="number"
    min="0"
    max="1"
    step="0.05"
    value="0.7"
    disabled
    required
>

</label>


</div>


<label>

Init image

<input
    id="sd-init"
    type="file"
    accept="
        image/png,
        image/jpeg,
        image/webp
    "
>

</label>


<img
    id="init-preview"
    hidden
>


<button
    id="clear-init"
    class="sd-button"
    type="button"
    hidden
>

Премахни изображението

</button>


<p>

512×512 и batch 1
са фиксирани.

Strength работи само
с init image.

</p>


<button
    id="sd-generate"
    class="sd-button"
    type="submit"
>

Generate

</button>


</form>


<p id="sd-status">

Готово за генериране.

</p>


<div
    id="sd-result"
    class="sd-preview"
>

Резултатът ще се появи тук.

</div>


</div>

</section>


<!-- ========================================================
     CHAT
     ======================================================== -->

<div class="chat-container">


<div class="chat-header">


<div class="chat-title">


<div class="online-dot"></div>


<span>

Локален AI Асистент

</span>


</div>


<button
    id="new-chat-button"
    class="new-chat-button"
    type="button"
>

Нов чат

</button>


</div>


<div
    class="chat-messages"
    id="chat-messages"
>


<div
    class="typing"
    id="typing-indicator"
>

Мисли...

</div>


</div>


<div
    class="memory-info"
    id="memory-info"
>

SQLite памет активна

</div>


<div class="chat-input-area">


<input
    type="text"
    id="chat-input"
    placeholder="Напишете съобщение..."
    autocomplete="off"
>


<button
    id="send-button"
    class="send-button"
    type="button"
>

➤

</button>


</div>


</div>


</main>


<script>


// ==========================================================
// SD SERVER
// ==========================================================

const sdForm =
    document.getElementById(
        "sd-form"
    );


const initInput =
    document.getElementById(
        "sd-init"
    );


const initPreview =
    document.getElementById(
        "init-preview"
    );


const clearInit =
    document.getElementById(
        "clear-init"
    );


const strengthInput =
    document.getElementById(
        "sd-strength"
    );


const sdStatus =
    document.getElementById(
        "sd-status"
    );


let previewUrl = null;


function updateInit() {


    if (previewUrl) {

        URL.revokeObjectURL(
            previewUrl
        );

        previewUrl = null;
    }


    const file =
        initInput.files[0];


    initPreview.hidden =
        !file;


    clearInit.hidden =
        !file;


    strengthInput.disabled =
        !file;


    if (file) {


        previewUrl =
            URL.createObjectURL(
                file
            );


        initPreview.src =
            previewUrl;


    } else {


        initPreview.removeAttribute(
            "src"
        );
    }
}


initInput.addEventListener(
    "change",
    updateInit
);


clearInit.addEventListener(
    "click",
    () => {


        initInput.value =
            "";


        updateInit();
    }
);


sdForm.addEventListener(
    "submit",

    async event => {


        event.preventDefault();


        const button =
            document.getElementById(
                "sd-generate"
            );


        if (button.disabled) {

            return;
        }


        button.disabled =
            true;


        sdStatus.textContent =
            "Генериране…";


        try {


            const file =
                initInput.files[0];


            if (
                file
                &&
                file.size >
                10 * 1024 * 1024
            ) {


                throw new Error(
                    "Init image трябва да е до 10 MB."
                );
            }


            const payload = {


                prompt:

                    document
                    .getElementById(
                        "sd-prompt"
                    )
                    .value
                    .trim(),


                negative_prompt:

                    document
                    .getElementById(
                        "sd-negative"
                    )
                    .value,


                steps:

                    Number(
                        document
                        .getElementById(
                            "sd-steps"
                        )
                        .value
                    ),


                cfg_scale:

                    Number(
                        document
                        .getElementById(
                            "sd-cfg"
                        )
                        .value
                    ),


                seed:

                    Number(
                        document
                        .getElementById(
                            "sd-seed"
                        )
                        .value
                    ),


                strength:

                    Number(
                        strengthInput.value
                    )
            };


            if (file) {


                payload.init_image =
                    await new Promise(
                        (
                            resolve,
                            reject
                        ) => {


                            const reader =
                                new FileReader();


                            reader.onload =
                                () =>
                                    resolve(
                                        reader.result
                                    );


                            reader.onerror =
                                () =>
                                    reject(
                                        new Error(
                                            "Неуспешно прочитане на изображението."
                                        )
                                    );


                            reader.readAsDataURL(
                                file
                            );
                        }
                    );
            }


            const response =
                await fetch(
                    "/api/generate",
                    {

                        method:
                            "POST",

                        headers:
                            {
                                "Content-Type":
                                    "application/json"
                            },

                        body:
                            JSON.stringify(
                                payload
                            )
                    }
                );


            const data =
                await response.json();


            if (!response.ok) {


                throw new Error(
                    data.error
                    ||
                    "Грешка при генерация."
                );
            }


            const result =
                document.getElementById(
                    "sd-result"
                );


            result.replaceChildren();


            data.images.forEach(
                (
                    encoded,
                    index
                ) => {


                    const img =
                        document.createElement(
                            "img"
                        );


                    img.src =
                        "data:image/png;base64,"
                        +
                        encoded;


                    const link =
                        document.createElement(
                            "a"
                        );


                    link.href =
                        img.src;


                    link.download =
                        "generated-"
                        +
                        index
                        +
                        ".png";


                    link.textContent =
                        "Изтегли PNG";


                    result.append(
                        img,
                        document.createElement(
                            "br"
                        ),
                        link,
                        document.createElement(
                            "br"
                        )
                    );
                }
            );


            sdStatus.textContent =
                "Готово."
                +
                (
                    data.seed != null
                    ?
                    " Seed: "
                    +
                    data.seed
                    :
                    ""
                );
        }


        catch (error) {


            sdStatus.textContent =
                "Грешка: "
                +
                error.message;
        }


        finally {


            button.disabled =
                false;
        }
    }
);


// ==========================================================
// CHAT UI
// ==========================================================

const messagesContainer =
    document.getElementById(
        "chat-messages"
    );


const chatInput =
    document.getElementById(
        "chat-input"
    );


const typingIndicator =
    document.getElementById(
        "typing-indicator"
    );


const sendButton =
    document.getElementById(
        "send-button"
    );


const newChatButton =
    document.getElementById(
        "new-chat-button"
    );


const memoryInfo =
    document.getElementById(
        "memory-info"
    );


function createMessageElement(
    text,
    sender
) {


    const div =
        document.createElement(
            "div"
        );


    div.classList.add(
        "message",
        sender
    );


    div.innerText =
        text;


    return div;
}


function addMessage(
    text,
    sender
) {


    messagesContainer.insertBefore(

        createMessageElement(
            text,
            sender
        ),

        typingIndicator
    );


    messagesContainer.scrollTop =
        messagesContainer.scrollHeight;
}


function clearMessages() {


    const items =
        messagesContainer
        .querySelectorAll(
            ".message"
        );


    items.forEach(
        item =>
            item.remove()
    );
}


function showWelcomeMessage() {


    addMessage(

        "Здравей! Аз съм твоят локален AI асистент. Разговорът вече се пази постоянно в SQLite. 🧠",

        "bot"
    );
}


// ==========================================================
// HISTORY
// ==========================================================

async function loadHistory() {


    try {


        const response =
            await fetch(
                "/api/history"
            );


        const data =
            await response.json();


        clearMessages();


        if (
            !data.messages
            ||
            data.messages.length === 0
        ) {


            showWelcomeMessage();


            memoryInfo.textContent =
                "SQLite памет активна · 0 съобщения";


            return;
        }


        for (
            const message
            of data.messages
        ) {


            if (
                message.role ===
                "user"
            ) {


                addMessage(
                    message.content,
                    "user"
                );


            } else if (
                message.role ===
                "assistant"
            ) {


                addMessage(
                    message.content,
                    "bot"
                );
            }
        }


        memoryInfo.textContent =
            "SQLite памет активна · "
            +
            data.messages.length
            +
            " съобщения";
    }


    catch (error) {


        clearMessages();


        showWelcomeMessage();


        memoryInfo.textContent =
            "Грешка при зареждане на паметта";
    }
}


// ==========================================================
// SEND MESSAGE
// ==========================================================

async function sendMessage() {


    const text =
        chatInput
        .value
        .trim();


    if (!text) {

        return;
    }


    if (
        sendButton.disabled
    ) {

        return;
    }


    addMessage(
        text,
        "user"
    );


    chatInput.value =
        "";


    typingIndicator.style.display =
        "block";


    sendButton.disabled =
        true;


    messagesContainer.scrollTop =
        messagesContainer.scrollHeight;


    try {


        const response =
            await fetch(
                "/api/chat",
                {

                    method:
                        "POST",

                    headers:
                        {
                            "Content-Type":
                                "application/json"
                        },

                    body:
                        JSON.stringify(
                            {
                                message:
                                    text
                            }
                        )
                }
            );


        const data =
            await response.json();


        typingIndicator.style.display =
            "none";


        if (!response.ok) {


            throw new Error(
                data.error
                ||
                "Моделът не отговори."
            );
        }


        addMessage(
            data.reply,
            "bot"
        );


        memoryInfo.textContent =
            "SQLite памет · "
            +
            data.database_messages
            +
            " съобщения · ~"
            +
            data.context_tokens
            +
            " tokens в текущия контекст";
    }


    catch (error) {


        typingIndicator.style.display =
            "none";


        addMessage(

            "Грешка: "
            +
            error.message,

            "bot"
        );
    }


    finally {


        sendButton.disabled =
            false;


        chatInput.focus();
    }
}


// ==========================================================
// NEW CHAT
// ==========================================================

async function resetChat() {


    const answer =
        confirm(
            "Да изтрия ли текущата история от SQLite?"
        );


    if (!answer) {

        return;
    }


    newChatButton.disabled =
        true;


    try {


        const response =
            await fetch(
                "/api/chat/reset",
                {
                    method:
                        "POST"
                }
            );


        const data =
            await response.json();


        if (!response.ok) {


            throw new Error(
                data.error
                ||
                "Неуспешно изчистване."
            );
        }


        clearMessages();


        showWelcomeMessage();


        memoryInfo.textContent =
            "SQLite памет активна · 0 съобщения";
    }


    catch (error) {


        alert(
            "Грешка: "
            +
            error.message
        );
    }


    finally {


        newChatButton.disabled =
            false;


        chatInput.focus();
    }
}


sendButton.addEventListener(
    "click",
    sendMessage
);


newChatButton.addEventListener(
    "click",
    resetChat
);


chatInput.addEventListener(
    "keydown",
    event => {


        if (
            event.key ===
            "Enter"
        ) {


            event.preventDefault();


            sendMessage();
        }
    }
);


loadHistory();


</script>


</body>

</html>
"""

    return render_template_string(
        html_content
    )


# ============================================================
# HISTORY API
# ============================================================

@app.route(
    "/api/history",
    methods=["GET"]
)
def api_history():
    chat_id = get_chat_id()

    with chat_lock:
        messages = get_messages(
            chat_id
        )

    return jsonify(
        {
            "messages":
                [
                    {
                        "role":
                            message["role"],

                        "content":
                            message["content"],

                        "created_at":
                            message["created_at"]
                    }
                    for message
                    in messages
                ]
        }
    )


# ============================================================
# RESET CHAT
# ============================================================

@app.route(
    "/api/chat/reset",
    methods=["POST"]
)
def reset_chat():
    chat_id = get_chat_id()

    with chat_lock:
        clear_messages(
            chat_id
        )

    return jsonify(
        {
            "ok":
                True
        }
    )


# ============================================================
# CHAT API
# ============================================================

@app.route(
    "/api/chat",
    methods=["POST"]
)
def ai_chat_endpoint():
    data = request.get_json(
        silent=True
    )

    if not isinstance(
            data,
            dict
    ):
        return jsonify(
            {
                "error":
                    "Очаква се JSON."
            }
        ), 400

    user_message = data.get(
        "message",
        ""
    )

    if not isinstance(
            user_message,
            str
    ):
        return jsonify(
            {
                "error":
                    "Съобщението трябва да е текст."
            }
        ), 400

    user_message = (
        user_message.strip()
    )

    if not user_message:
        return jsonify(
            {
                "error":
                    "Празно съобщение."
            }
        ), 400

    if len(
            user_message
    ) > 50000:
        return jsonify(
            {
                "error":
                    "Съобщението е прекалено дълго."
            }
        ), 400

    chat_id = get_chat_id()

    with chat_lock:

        # ====================================================
        # 1. Записваме user message в SQLite
        # ====================================================

        add_message(
            chat_id,
            "user",
            user_message
        )

        # ====================================================
        # 2. Зареждаме историята от SQLite
        # ====================================================

        history = build_context(
            chat_id
        )

        try:

            # =================================================
            # 3. Даваме историята на llama.cpp
            # =================================================

            response = (
                ai_client
                .chat
                .completions
                .create(
                    model=
                    "local-model",

                    messages=
                    history,

                    temperature=
                    0.7,

                    max_tokens=
                    MAX_RESPONSE_TOKENS
                )
            )

            bot_reply = ""

            if (
                    hasattr(
                        response,
                        "choices"
                    )
                    and
                    response.choices
            ):

                choice = (
                    response.choices[0]
                )

                if (
                        hasattr(
                            choice,
                            "message"
                        )
                        and
                        hasattr(
                            choice.message,
                            "content"
                        )
                ):

                    bot_reply = (
                        choice
                        .message
                        .content
                    )


                elif isinstance(
                        choice,
                        dict
                ):

                    bot_reply = (
                        choice
                        .get(
                            "message",
                            {}
                        )
                        .get(
                            "content",
                            ""
                        )
                    )


                else:

                    bot_reply = getattr(
                        choice,
                        "text",
                        ""
                    )

            if bot_reply is None:
                bot_reply = ""

            bot_reply = (
                str(
                    bot_reply
                )
                .strip()
            )

            if not bot_reply:
                raise RuntimeError(
                    "Моделът върна празен отговор."
                )

            # =================================================
            # 4. Записваме assistant reply в SQLite
            # =================================================

            add_message(
                chat_id,
                "assistant",
                bot_reply
            )

            # =================================================
            # 5. Статистика
            # =================================================

            all_messages = get_messages(
                chat_id
            )

            current_context = build_context(
                chat_id
            )

            context_tokens = (
                estimate_history_tokens(
                    current_context
                )
            )

            print(
                f"[CHAT] "
                f"session={chat_id[:8]} "
                f"db_messages={len(all_messages)} "
                f"context_messages={len(current_context) - 1} "
                f"estimated_tokens={context_tokens}"
            )

            return jsonify(
                {
                    "reply":
                        bot_reply,

                    "database_messages":
                        len(all_messages),

                    "context_tokens":
                        context_tokens
                }
            )


        except Exception as exc:

            print(
                f"Грешка с llama.cpp: {exc}"
            )

            # Ако моделът се срине,
            # махаме последното user съобщение,
            # за да не остане половин разговор.

            connection = (
                get_db_connection()
            )

            try:

                row = connection.execute(
                    """
                    SELECT id
                    FROM messages

                    WHERE conversation_id = ?
                      AND role = 'user'

                    ORDER BY id DESC LIMIT 1
                    """,
                    (
                        chat_id,
                    )
                ).fetchone()

            finally:

                connection.close()

            if row:
                delete_message_by_id(
                    row["id"]
                )

            return jsonify(
                {
                    "error":
                        "Локалният модел не отговори правилно."
                }
            ), 500


# ============================================================
# IMAGE GENERATION
# ============================================================

@app.route(
    "/api/generate",
    methods=["POST"]
)
def generate_image():
    data = request.get_json(
        silent=True
    )

    if not isinstance(
            data,
            dict
    ):
        return jsonify(
            error=
            "Очаква се JSON обект."
        ), 400

    try:

        prompt = data.get(
            "prompt",
            ""
        )

        negative = data.get(
            "negative_prompt",
            ""
        )

        if (
                not isinstance(
                    prompt,
                    str
                )
                or
                not prompt.strip()
        ):
            raise ValueError(
                "Въведете prompt."
            )

        if not isinstance(
                negative,
                str
        ):
            raise ValueError(
                "Negative prompt трябва да е текст."
            )

        def number(
                name,
                default,
                low,
                high,
                integer=False
        ):

            value = data.get(
                name,
                default
            )

            if (
                    isinstance(
                        value,
                        bool
                    )
                    or
                    not isinstance(
                        value,
                        (
                                int,
                                float
                        )
                    )
            ):
                raise ValueError(
                    f"Невалидна стойност за {name}."
                )

            if (
                    not math.isfinite(
                        value
                    )
                    or
                    not low <= value <= high
                    or
                    (
                            integer
                            and
                            value != int(
                        value
                    )
                    )
            ):
                raise ValueError(
                    f"{name} трябва да е между {low} и {high}."
                )

            if integer:
                return int(
                    value
                )

            return value

        payload = {

            "prompt":
                prompt.strip(),

            "negative_prompt":
                negative,

            "width":
                512,

            "height":
                512,

            "batch_size":
                1,

            "steps":
                number(
                    "steps",
                    20,
                    1,
                    100,
                    True
                ),

            "cfg_scale":
                number(
                    "cfg_scale",
                    3.5,
                    0,
                    30
                ),

            "seed":
                number(
                    "seed",
                    -1,
                    -1,
                    2147483647,
                    True
                )
        }

        endpoint = (
            "/sdapi/v1/txt2img"
        )

        init = data.get(
            "init_image"
        )

        if init:

            if (
                    not isinstance(
                        init,
                        str
                    )
                    or
                    "," not in init
            ):
                raise ValueError(
                    "Невалидно init image."
                )

            header, encoded = (
                init.split(
                    ",",
                    1
                )
            )

            if header not in (
                    "data:image/png;base64",
                    "data:image/jpeg;base64",
                    "data:image/webp;base64"
            ):
                raise ValueError(
                    "Изберете PNG, JPEG или WebP."
                )

            try:

                raw = (
                    base64.b64decode(
                        encoded,
                        validate=True
                    )
                )

            except Exception:

                raise ValueError(
                    "Невалидно кодиране на init image."
                )

            if (
                    not raw
                    or
                    len(raw)
                    >
                    10 * 1024 * 1024
            ):
                raise ValueError(
                    "Init image трябва да е до 10 MB."
                )

            payload[
                "init_images"
            ] = [
                encoded
            ]

            payload[
                "denoising_strength"
            ] = number(
                "strength",
                0.7,
                0,
                1
            )

            endpoint = (
                "/sdapi/v1/img2img"
            )


    except ValueError as exc:

        return jsonify(
            error=
            str(exc)
        ), 400

    if not sd_lock.acquire(
            blocking=False
    ):
        return jsonify(
            error=
            "Вече се генерира изображение. Изчакайте."
        ), 409

    try:

        response = requests.post(

            SD_URL + endpoint,

            json=
            payload,

            timeout=
            (
                5,
                1800
            )
        )

        if not response.ok:
            return jsonify(
                error=
                f"sd-server HTTP "
                f"{response.status_code}: "
                f"{response.text[:1000]}"
            ), 502

        result = (
            response.json()
        )

        images = result.get(
            "images"
        )

        if (
                not isinstance(
                    images,
                    list
                )
                or
                not images
                or
                not all(
                    isinstance(
                        image,
                        str
                    )
                    and
                    image
                    for image
                    in images
                )
        ):
            return jsonify(
                error=
                "sd-server не върна изображение."
            ), 502

        info = result.get(
            "info",
            {}
        )

        if isinstance(
                info,
                str
        ):

            import json

            try:

                info = json.loads(
                    info
                )

            except ValueError:

                info = {}

        if isinstance(
                info,
                dict
        ):

            seed = info.get(
                "seed"
            )

        else:

            seed = None

        return jsonify(
            images=
            images,

            seed=
            seed
        )


    except requests.Timeout:

        return jsonify(
            error=
            "Времето за изчакване изтече. "
            "sd-server може още да генерира."
        ), 504


    except requests.ConnectionError:

        return jsonify(
            error=
            "Няма връзка със sd-server на порт 8081."
        ), 502


    except (
            ValueError,
            requests.RequestException
    ):

        return jsonify(
            error=
            "Невалиден отговор от sd-server."
        ), 502


    finally:

        sd_lock.release()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    print()
    print(
        "============================================"
    )
    print(
        " LOCAL AI + FLUX + SQLITE MEMORY"
    )
    print(
        "============================================"
    )
    print(
        f"Database: {DB_PATH}"
    )
    print(
        "Web UI:   http://127.0.0.1:5005"
    )
    print(
        "Llama:    http://127.0.0.1:8080"
    )
    print(
        "SD:       http://127.0.0.1:8081"
    )
    print(
        "============================================"
    )
    print()

    app.run(
        host=
        "127.0.0.1",

        port=
        5005,

        debug=
        False,

        threaded=
        True
    )
