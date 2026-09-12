from flask import Flask, request, jsonify
from flask_cors import CORS  # ВАЖНО: Разрешава на сайта ти да комуникира с това приложение
from openai import OpenAI

app = Flask(__name__)
CORS(app)  # Активираме CORS за сигурни заявки между различни портове

# Свързване с твоя локален llama.cpp сървър
ai_client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="local-llama"
)


@app.route('/api/external-chat', methods=['POST'])
def external_chat():
    data = request.get_json()
    user_message = data.get('message', '')

    if not user_message:
        return jsonify({'error': 'Празно съобщение'}), 400

    try:
        response = ai_client.chat.completions.create(
            model="local-model",
            messages=[
                {"role": "system",
                 "content": "Ти си любезен асистент в български магазин за железария. Отговаряй кратко и само на български."},
                {"role": "user", "content": user_message}
            ],
            temperature=0.7
        )
        bot_reply = response.choices.message.content
        return jsonify({'reply': bot_reply})

    except Exception as e:
        print(f"Грешка с llama.cpp: {e}")
        return jsonify({'error': 'Локалният модел не отговори'}), 500


if __name__ == '__main__':
    # Пускаме го на порт 5005, за да не си пречи с основния сайт (порт 5000)
    app.run(port=5005, debug=True)
