DARSLIK STUDIYASI — BULUTLI SERVER (persistent job tizimi)
=============================================================

YANGILANISH (ikkinchi bosqich)
--------------------------------
Bu versiyaga qo'shildi:
  - Takrorlanish/sukut aniqlash endi jarayonni to'xtatmaydi - faqat "shubhali
    joy" sifatida bo'lak va vaqt ko'rsatilgan holda belgilanadi.
  - Har bir bo'lak (segment) uchun to'liq tafsilot: raqami, vaqti, davomiyligi,
    matni, holati, aniqlangan muammolar.
  - "Tahrirlash va audio": tarjima bo'laklarini birma-bir ko'rish/tahrirlash,
    yangi fayldan bo'lak almashtirish, faqat o'zgargan bo'laklarning audiosi
    qayta yaratiladi (eskilari saqlanadi - API xarajati va vaqt tejaladi).
  - Yakuniy video Darslik Studiyasining o'zida pleyer orqali ko'riladi:
    Audio (Original / O'zbekcha) va Subtitr (Original / O'zbekcha / O'chirilgan)
    treklarini almashtirish, playback tezligi (0.5x-2x) - hech narsa qayta
    yuklanmaydi.
  - Subtitr videoga "kuydirilmaydi" (burn-in yo'q) - alohida WebVTT trek
    sifatida ishlaydi, shuning uchun video hajmi asossiz oshmaydi.
  - Menyu soddalashtirildi: "Videolar" markaziy bo'lim, "Video→Matn",
    "Matn/Tarjima", "Audio" endi asosiy menyuda ko'rinmaydi (funksiyalari
    video sahifasining o'zida ishlaydi), "Xarajatlar" Sozlamalar ichida.
  - "Avtomatik tarjima" tugmasi UI'dan yashirilgan (backend saqlangan).

Bu versiyada frontend endi faqat boshqaruv paneli. Barcha og'ir ish —
video yuklash, ffmpeg preprocessing, Whisper transkripsiya, TTS — serverda
persistent job sifatida bajariladi. Brauzerni yopsangiz, telefon o'chsa,
internet uzilsa yoki Railway serverni qayta ishga tushirsa ham, ish
to'xtagan joyidan davom etadi.

FAYLLAR
--------
    app.py              - FastAPI endpointlar (asosiy kirish nuqtasi)
    database.py          - SQLite persistence qatlami
    storage.py            - papka joylashuvi va konfiguratsiya (env vars)
    keys_manager.py        - OpenAI API kalitlar (shifrlangan, rotatsiya)
    transcription.py        - ffmpeg, glossary, Whisper, takrorlanish aniqlash
    worker.py                - video queue: preprocessing + transkripsiya
    tts.py                    - Matn->Audio backend job (Aisha/OpenAI TTS)
    glossary_data.py           - stomatologik lug'at (o'zgarmagan)
    index.html                  - frontend (boshqaruv paneli)
    requirements.txt, Procfile

TAYYORGARLIK (bir martalik)
----------------------------
1. https://railway.com saytida ro'yxatdan o'ting.
2. Node.js o'rnating (agar yo'q bo'lsa), keyin:

       npm install -g @railway/cli

PERSISTENT VOLUME QO'SHISH (MUHIM — bir martalik)
----------------------------------------------------
Bu versiya videolarni, natijalarni va bazani doimiy saqlash uchun
Railway'ning **persistent volume** funksiyasidan foydalanadi. Buni
albatta sozlang, aks holda har deploy/restartda barcha ma'lumot yo'qoladi:

1. Railway loyihangizni oching -> xizmatingizni tanlang -> "Settings" ->
   "Volumes" bo'limi -> "New Volume".
2. Mount path sifatida shuni yozing:

       /data

3. Hajmni kamida 100 GB qilib belgilang (spetsifikatsiyaga mos).

ENVIRONMENT VARIABLE'LAR
--------------------------
Railway "Variables" bo'limida quyidagilarni sozlang:

    STORAGE_DIR=/data
        Yuqorida yaratgan volume mount pathi bilan bir xil bo'lishi shart.

    APP_SECRET=<istalgan uzun tasodifiy matn>
        API kalitlarni shifrlash uchun. Bermasangiz ham ishlaydi (server
        o'zi tasodifiy kalit yaratib STORAGE_DIR ichida saqlaydi), lekin
        aniq belgilash tavsiya etiladi (ayniqsa bir nechta instance
        ishlatsangiz).

    ADMIN_TOKEN=<ixtiyoriy>
        Agar panel internetga ochiq bo'lsa va video o'chirishni himoya
        qilmoqchi bo'lsangiz, shu yerga token qo'ying. Frontendda hozircha
        ishlatilmaydi (kerak bo'lsa qo'shiladi) — API'ni to'g'ridan-to'g'ri
        chaqirishdan himoya beradi.

    AISHA_API_BASE=<Aisha API asosiy manzili>
        DIQQAT: asl loyihangizdagi index.html faylida "AISHA_BASE" ishlatilgan,
        lekin u hech qayerda aniqlanmagan edi (kod ichida topilmadi). Aisha
        TTS ishlashi uchun bu manzilni albatta to'g'ri qiymat bilan to'ldiring
        (masalan Aisha hujjatlaridan yoki ilgari ishlatgan manzildan oling).

    Quyidagilar ixtiyoriy — standart qiymatlar spetsifikatsiyaga mos:

    CHUNK_SECONDS=300              (5 daqiqalik audio bo'lak)
    MAX_WHISPER_CONCURRENCY=4      (bitta video ichida parallel Whisper so'rovi)
    MAX_ACTIVE_VIDEO_JOBS=1        (bir vaqtda nechta video faol ishlansin)
    MAX_ACTIVE_TTS_JOBS=1          (bir vaqtda nechta TTS ish faol ishlansin)
    MAX_UPLOAD_SIZE=21474836480    (20 GB, baytlarda)
    STORAGE_LIMIT=102005473280     (95 GB, baytlarda — disk to'lib ketmasligi uchun)
    REPETITION_THRESHOLD=3         (necha marta ketma-ket takrorlansa shubhali)
    UPLOAD_CHUNK_SIZE=8388608      (8 MB — faqat ma'lumot uchun, frontend o'zi belgilaydi)

JOYLASHTIRISH
--------------
1. Ushbu papkani (barcha .py fayllar, index.html, glossary_data.py,
   requirements.txt, Procfile) kompyuteringizga saqlang.
2. cmd/terminalda shu papkaga o'ting.
3. Railway hisobingizga kiring:

       railway login

4. Yangi loyiha yarating (agar hali yaratmagan bo'lsangiz):

       railway init

5. Yuqoridagi "Persistent volume" va "Environment variable"larni Railway
   saytida sozlang (bir martalik).
6. Joylashtiring:

       railway up

7. "Settings" -> "Generate Domain" orqali manzil oling. Frontend endi
   backend bilan bir xil serverda ishlaydi — alohida "server manzili"
   kiritish shart emas, faqat shu domenni brauzerda oching.

YANGILASH (kelajakda kod o'zgarganda)
----------------------------------------
Xuddi shu papkada turib:

    railway up

Volume ichidagi ma'lumot (videolar, natijalar, baza) saqlanib qoladi.

ASOSIY ISHLASH PRINSIPI (yangi arxitektura)
----------------------------------------------
Bitta video = bitta loyiha. Har bir video quyidagi bosqichlardan ketma-ket
o'tadi, har biri video kartasi/sahifasida aniq ko'rinadi:

  1. Video serverga yuklanadi (resumable/chunked) -> "uploaded".
     Bu bosqichda darhol thumbnail va davomiylik olinadi.
  2. Foydalanuvchi "Bo'laklarga bo'lish"ni bosadi -> "segmenting" -> "segments_ready".
     Bu bosqichda hali OpenAI'ga hech narsa yuborilmaydi.
  3. "Video → Matn" bo'limida videoni tanlab, tilni belgilab
     "Transkripsiyani boshlash"ni bosadi -> "transcribing" -> "transcription_ready".
     Bo'lak-darajasidagi progress, xato/takrorlanishda pauza, API kalit
     tugaganda pauza - barchasi shu bosqichda ishlaydi.
  4. Foydalanuvchi tayyor matnni tekshirib "Tasdiqlash"ni bosadi ->
     "transcription_approved". Tasdiqlanmagan matn bilan tarjima/audio
     bosqichi boshlanmaydi.
  5. "Matn / Tarjima" bo'limida avtomatik tarjima yoki tayyor matn/fayl
     yuklanadi -> "translation_ready".
  6. "Audio" bo'limida provayder (Aisha/OpenAI) tanlanib audio yaratiladi ->
     "audio_processing" -> "audio_ready".
  7. "Videoga audio qo'shish" bosiladi -> "video_rendering" -> "completed".
     Server original video tasvirini saqlab, audio yo'lini yangi audio bilan
     almashtiradi (ffmpeg, video qayta kodlanmaydi - tez ishlaydi).

Har bir bosqichda xato yoki to'xtash sababi (blocked_reason) alohida
ko'rsatiladi: "paused" (foydalanuvchi to'xtatgan), "api_key" (kalit kerak),
"repetition" (Whisper takrorlanishi), "chunk_errors" (ba'zi bo'laklar xato),
"error" (umumiy xato). Har biri "Davom ettirish"/"Qayta urinish" bilan
davom ettiriladi.

Server qayta ishga tushsa (Railway restart), faol (blocked_reason=None)
bosqichlar avtomatik davom ettiriladi; foydalanuvchi ataylab to'xtatgan
yoki xatoga uchragan bosqichlar esa qo'lda "Davom ettirish" kutadi -
bu ataylab shunday qilingan, aks holda foydalanuvchining pauzasi
e'tiborsiz qoldirilardi.

Xarajatlar (Whisper, tarjima, TTS) avtomatik hisoblanadi va "Xarajatlar"
bo'limida video kesimida hamda kunlik/haftalik/oylik/jami ko'rinishda
chiqadi.

ESKI VERSIYADAN FARQI
------------------------
- Video yuklangach endi AVTOMATIK bo'laklarga bo'linmaydi - bu alohida,
  foydalanuvchi boshqaradigan qadam.
- Yangi "Matn / Tarjima" bosqichi qo'shildi (avtomatik yoki qo'lda tarjima).
- "Videoga audio qo'shish" (yakuniy video yig'ish) endi serverda ishlaydi
  (avval faqat brauzerda, ffmpeg.wasm bilan bo'lardi - eski "Birlashtirish"
  sahifasi hozir ham mavjud, lekin menyuda yashirilgan, agar kerak bo'lsa
  index.html ichida uni qayta ko'rsatish mumkin).
- Interfeys markazi endi "Server videolari" - har video uchun bitta karta,
  bosilganda butun pipeline (segmentlardan yakuniy videogacha) bitta joyda
  ko'rinadi.
- Yuqorida kichik server holati indikatori (🟢/🔴) va tezkor "API"/"Aysha"
  sozlash tugmalari qo'shildi.

TEKSHIRILGAN STSENARIYLAR
----------------------------
Ushbu versiya to'liq pipeline bo'yicha sinovdan o'tkazildi (upload -> segment
-> transcribe -> approve -> translate -> audio -> render -> completed),
shu jumladan yakuniy video yuklab olish endpointi. Server "qulashi"
simulyatsiyasi orqali qayta tiklanish tekshirildi (video_rendering
bosqichida). Haqiqiy OpenAI/Aisha API kalitlari bilan to'liq sinov
o'tkazilmadi - ishlatishdan oldin qisqa video bilan sinab ko'rishni
tavsiya qilamiz.

