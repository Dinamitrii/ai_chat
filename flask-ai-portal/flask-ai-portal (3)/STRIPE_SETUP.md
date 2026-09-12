# Stripe — месечен платен достъп

Кодът е подготвен за един месечен абонамент с фиксирана цена. Цената и валутата са ваш избор в Stripe. Не са създавани продукти или плащания във ваш акаунт. Първо направете целия тест със sandbox/test ключове.

## 1. Обновете приложението

Запазете папката `instance/`. Заменете `app.py` и `requirements.txt`, после във виртуалната среда:

```bash
python -m pip install -r requirements.txt
```

Новите таблици се създават автоматично. Старият дизайн, квотите, чатът и генерацията на изображения остават.

## 2. Създайте продукт и месечна цена

В [Stripe Dashboard](https://dashboard.stripe.com/) изберете sandbox/test средата. В продуктовия каталог създайте продукт, например „AI Paid“, и Price с:

- recurring / monthly, на всеки 1 месец;
- фиксирана положителна сума и избрана валута;
- количество 1, без metered usage, trial или промоционални кодове.

Копирайте `price_...` идентификатора на цената, а не `prod_...` на продукта. В Developer/API keys вземете тестовия Secret key `sk_test_...`. Publishable `pk_...` ключ не е необходим за този hosted Checkout вариант.

Не изпращайте Secret key в чата и не го записвайте в `app.py`.

## 3. Локален webhook

Инсталирайте [Stripe CLI по официалните инструкции](https://docs.stripe.com/stripe-cli). В отделен терминал:

```bash
stripe login
stripe listen --forward-to http://127.0.0.1:5005/webhooks/payment
```

Оставете командата да работи. Тя показва signing secret `whsec_...`; използвайте точно него за локалния listener. Той е различен от signing secret на endpoint, създаден в Dashboard.

## 4. Конфигурирайте и стартирайте

В терминала, от който ще стартирате Flask, задайте следните променливи. Стойностите по-долу са заместители, които трябва да замените локално:

```bash
export STRIPE_SECRET_KEY='sk_test_REPLACE_ME'
export STRIPE_WEBHOOK_SECRET='whsec_REPLACE_ME'
export STRIPE_PRICE_ID='price_REPLACE_ME'
export STRIPE_LIVE_MODE=0
export PUBLIC_URL='http://127.0.0.1:5005'
export COOKIE_SECURE=0
export AI_DATA_DIR="$PWD/instance-stripe-test"
python app.py
```

Тестът използва отделна папка, затова регистрирайте тестов потребител. Съществуващите ви данни в `instance/` остават непокътнати. Променливите важат за текущия терминал. Приложението не зарежда `.env` автоматично. При systemd/gunicorn ги задайте в средата на съответната услуга.

## 5. Направете тест през самото приложение

1. Влезте с тестов акаунт и отворете Upgrade.
2. Натиснете „Абонирай се чрез Stripe“.
3. Проверете показаните цена, валута и месечна периодичност.
4. В тестовия Checkout използвайте карта `4242 4242 4242 4242`, бъдеща дата и произволен трицифрен CVC. Не въвеждайте истинска карта в тестов режим.
5. След плащането ще се върнете в Upgrade. Webhook-ът трябва да активира `paid`; натиснете „Провери плащането“, ако страницата още показва `free`.
6. Проверете, че в терминала на Stripe CLI известията получават HTTP 200. Временен 409 при едновременен Checkout/webhook може да се повтори; бутонът за проверка извършва същата проверка през API.

[Официални Stripe тестови карти](https://docs.stripe.com/testing?testing-method=card-numbers).

Простото отваряне на `/upgrade?checkout=success` никога не отключва paid. За тест на реалната връзка използвайте Checkout, създаден от приложението: произволно `stripe trigger invoice.paid` създава данни без локалната връзка към акаунта.

## 6. Customer Portal

Активирайте [Customer Portal](https://docs.stripe.com/customer-management/activate-no-code-customer-portal) в същия Stripe режим. Разрешете:

- преглед на фактурите;
- обновяване на платежния метод;
- прекратяване в края на текущия платен период.

Оставете изключени смяна на продукт/цена/количество и retention coupons. Текущата версия поддържа един фиксиран месечен Price, без prorations или отстъпки. Бутонът „Управлявай абонамента“ отваря персоналния портал на съответния клиент.

## 7. Поведение на достъпа

- Проверен активен абонамент + платена фактура + успешно, неподлежащо на refund/dispute плащане → `paid` до края на периода.
- Прекратяване в края на периода → достъпът остава до тази дата.
- Незабавно прекратяване, unpaid/past_due, refund (включително частичен) или dispute → достъпът се отнема при съответното известие и проверка.
- Няма гратисен период, trials, ваучери, ръчно маркиране „paid out of band“ или плащане от customer credit в тази версия. Такива случаи не отключват достъп автоматично.
- Изтеклият срок връща `free` и без webhook при следваща заявка. Забавен renewal webhook може временно да остави клиента free; „Провери плащането“ възстановява достъпа след проверка.
- Връщането към free не занулява lifetime usage. Ако free квотите вече са изчерпани, те остават изчерпани.
- Повторени webhook събития не се обработват два пъти. При събитие, дошло извън реда си, се използва текущото състояние от Stripe.

За възстановяване след пропуснати webhook събития операторът може да изпълни:

```bash
flask --app app sync-billing
```

Проверката на абонамента не се прави през Stripe при всяко чат съобщение: използва се локално записаният срок, за да не се забавя чатът. Затова навременното отнемане при refund/cancellation зависи от доставката на webhook или тази синхронизация.

## 8. Истински плащания

След успешен тест и активиран Stripe live акаунт:

1. Разположете приложението зад HTTPS на собствен домейн.
2. Създайте live продукт и live monthly Price. Test Price ID не работи с live ключ.
3. Създайте live webhook endpoint: `https://YOUR-DOMAIN/webhooks/payment`.
4. Абонирайте endpoint-а за:
   - `checkout.session.completed`
   - `checkout.session.async_payment_succeeded`
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.paid`
   - `invoice.payment_failed`
   - `charge.refunded`
   - `charge.dispute.created`
   - `charge.dispute.closed`
5. Задайте live `sk_live_...`, live `price_...` и `whsec_...` от този live endpoint, `STRIPE_LIVE_MODE=1`, `PUBLIC_URL=https://YOUR-DOMAIN`, `COOKIE_SECURE=1`.
6. Използвайте отделна постоянна `AI_DATA_DIR` за live. Не използвайте тестовата база. Може да използвате оригиналната `instance/`, ако там не сте правили Stripe тестове; така запазвате съществуващите потребители.
7. Активирайте и конфигурирайте Customer Portal и в live режима.

Кодът отказва live Checkout без HTTPS/Secure cookie. API заявките използват изрично Stripe API `2024-06-20`, за да получават стабилните invoice/charge полета; задайте и webhook endpoint със същата версия, когато е възможно. Пазете версията при обновяване и тествайте преди смяната ѝ. Не сменяйте Price ID/режима под действащи абонаменти без миграция на billing логиката.

## Проверки, изпълнени при подготовката

23 автоматични теста минаха. Stripe тестовете използват действителна локална HMAC проверка на подписа с тестова тайна и симулирани API отговори. Покриват неплатена фактура, грешен подпис/режим/цена/сума, успешно плащане, повторено събитие, refund, старо събитие, expiration, cancellation, Checkout повторения и временен API отказ.

Не е правено плащане във ваш Stripe акаунт. Преди live остава да направите описания тест през Checkout със своите test ключове и Price.

Източници: [Stripe webhooks](https://docs.stripe.com/webhooks?lang=python), [Subscription webhooks](https://docs.stripe.com/billing/subscriptions/webhooks), [API versioning](https://docs.stripe.com/api/versioning).
