from flask import Flask, request, jsonify, render_template_string
from openai import OpenAI

app = Flask(__name__)

# Свързване с вашия локален llama.cpp сървър
ai_client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="local-llama"
)


# 1. КОГАТО ОТВОРИТЕ САЙТА (GET заявка)
@app.route('/', methods=['GET'])
def home_page():
    # Вграден HTML и JavaScript директно в Python файла
    html_content = """
    <!DOCTYPE html>
    <html lang="bg">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Локален AI Асистент</title>
        <style>
            body { font-family: 'Segoe UI', Arial, sans-serif; background: #f0f2f5; margin: 0; padding: 0; display: flex; justify-content: center; align-items: center; height: 100vh; }
            .chat-container { width: 400px; height: 600px; background: white; border-radius: 16px; box-shadow: 0 8px 24px rgba(0,0,0,0.1); display: flex; flex-direction: column; overflow: hidden; border: 1px solid #e0e0e0; }
            .chat-header { background: #007bff; color: white; padding: 20px; font-weight: bold; font-size: 18px; display: flex; align-items: center; gap: 10px; }
            .online-dot { width: 10px; height: 10px; background: #2ecc71; border-radius: 50%; }
            .chat-messages { flex: 1; padding: 20px; overflow-y: auto; background: #f8f9fa; display: flex; flex-direction: column; gap: 15px; }
            .message { max-width: 80%; padding: 12px 16px; border-radius: 14px; font-size: 15px; line-height: 1.4; }
            .bot { align-self: flex-start; background: white; color: #333; border: 1px solid #e4e6eb; border-top-left-radius: 4px; }
            .user { align-self: flex-end; background: #007bff; color: white; border-top-right-radius: 4px; }
            .typing { align-self: flex-start; background: transparent; color: #777; font-style: italic; display: none; font-size: 14px; }
            .chat-input-area { padding: 15px; background: white; border-top: 1px solid #eee; display: flex; gap: 10px; }
            input { flex: 1; padding: 12px 18px; border: 1px solid #ccd0d5; border-radius: 24px; outline: none; font-size: 15px; }
            button { background: #007bff; color: white; border: none; width: 45px; height: 45px; border-radius: 50%; cursor: pointer; display: flex; align-items: center; justify-content: center; font-size: 18px; transition: background 0.2s; }
            button:hover { background: #0056b3; }
        </style>
    </head>
    <body>

    <div class="chat-container">
        <div class="chat-header">
            <div class="online-dot"></div>
            <span>Локален AI Асистент</span>
        </div>

        <div class="chat-messages" id="chat-messages">
            <div class="message bot">Здравейте! Аз съм Вашият локален AI асистент, захранван от llama.cpp. С какво мога да помогна? 🛠️</div>
            <div class="typing" id="typing-indicator">Мисли...</div>
        </div>

        <div class="chat-input-area">
            <input type="text" id="chat-input" placeholder="Напишете съобщение..." autocomplete="off">
            <button onclick="sendMessage()">➤</button>
        </div>
    </div>

    <script>
        const messagesContainer = document.getElementById('chat-messages');
        const chatInput = document.getElementById('chat-input');
        const typingIndicator = document.getElementById('typing-indicator');

        async function sendMessage() {
            const text = chatInput.value.trim();
            if (!text) return;

            // Показване на потребителското съобщение
            messagesContainer.insertBefore(createMessageElement(text, 'user'), typingIndicator);
            chatInput.value = '';
            messagesContainer.scrollTop = messagesContainer.scrollHeight;

            // Показване на индикатора за писане
            typingIndicator.style.display = 'block';

            try {
                // Изпращане на заявка към същия този Flask сървър (към POST маршрута)
                const response = await fetch('/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message: text })
                });

                const data = await response.json();
                typingIndicator.style.display = 'none';

                if (data.reply) {
                    messagesContainer.insertBefore(createMessageElement(data.reply, 'bot'), typingIndicator);
                } else {
                    throw new Error();
                }
            } catch (error) {
                typingIndicator.style.display = 'none';
                messagesContainer.insertBefore(createMessageElement('Грешка: Неуспешна връзка с модела.', 'bot'), typingIndicator);
            }

            messagesContainer.scrollTop = messagesContainer.scrollHeight;
        }

        function createMessageElement(text, sender) {
            const div = document.createElement('div');
            div.classList.add('message', sender);
            div.innerText = text;
            return div;
        }

        chatInput.addEventListener('keypress', (e) => { if (e.key === 'Enter') sendMessage(); });
    </script>

    </body>
    </html>
    """
    return render_template_string(html_content)


# 2. ПРИЕМАНЕ НА СЪОБЩЕНИЯТА ОТ ЧАТА (POST заявка)
@app.route('/api/chat', methods=['POST'])
def ai_chat_endpoint():
    data = request.get_json()
    user_message = data.get('message', '')

    if not user_message:
        return jsonify({'error': 'Празно съобщение'}), 400

    try:
        response = ai_client.chat.completions.create(
            model="local-model",
            messages=[
                {"role": "system",
                 "content": "Ти си любезен и кратък изкуствен интелект. Отговаряй винаги на български език."},
                {"role": "user", "content": user_message}
            ],
            temperature=0.7
        )

        # КОРИГИРАН РЕД: Безопасно извличане на отговора, независимо дали е обект или речник
        if hasattr(response, 'choices') and len(response.choices) > 0:
            choice = response.choices[0]
            # Проверяваме дали вътрешната структура е обект или речник (dict)
            if hasattr(choice, 'message') and hasattr(choice.message, 'content'):
                bot_reply = choice.message.content
            elif isinstance(choice, dict) and 'message' in choice:
                bot_reply = choice['message'].get('content', '')
            else:
                # Ако llama.cpp върне по-опростен формат директно в обекта
                bot_reply = getattr(choice, 'text', str(choice))
        else:
            # Алтернативно извличане, ако структурата е чист речник (dict)
            bot_reply = response['choices'][0]['message']['content']

        return jsonify({'reply': bot_reply})

    except Exception as e:


        print(f"Грешка с llama.cpp: {e}")
    return jsonify({'error': 'Локалният модел не отговори правилно'}), 500

if __name__ == '__main__':
    # Стартираме уеб сървъра на порт 5005
    print("Стартиране на чат сайта на адрес: http://localhost:5005")
    app.run(port=5005, debug=True)
