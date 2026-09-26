# بوت إدارة قروبات تيليجرام

## المتطلبات
- Python 3.11+
- Token من BotFather
- البوت مضاف كمشرف في القروب مع صلاحيات حذف الرسائل وتقييد/حظر الأعضاء.

## متغيرات البيئة
ضع في الاستضافة:
- `BOT_TOKEN` = توكن البوت
- `OWNER_ID` = رقم حساب المالك في تيليجرام

اختياري:
- `BOOTSTRAP_CODE` = رمز مؤقت إذا أردت تجهيز المالك بطريقة آمنة.

لا تضع التوكن داخل الكود ولا ترسله لأي شخص.

## تشغيل محلي
```bash
pip install -r requirements.txt
python bot.py
```

## أوامر أساسية
/start
/panel
/id
/idgroup
/addmanager رقم_المستخدم
/delmanager رقم_المستخدم
/managers
/ban (بالرد على رسالة)
/kick (بالرد)
/mute (بالرد)
/unmute (بالرد)
/del (بالرد)
/pin (بالرد)
/alerts

الحماية:
/locklinks /unlocklinks
/lockphoto /unlockphoto
/lockvideo /unlockvideo
/lockaudio /unlockaudio
/lockfile /unlockfile

ملاحظة: هذه نسخة بداية قابلة للتوسعة. قاعدة SQLite مناسبة للتجربة، لكن التخزين المحلي في بعض الاستضافات المجانية قد يكون مؤقتًا.
