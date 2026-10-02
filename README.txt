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
internet uzilsa yoki Oracle serveri qayta ishga tushsa ham, ish
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

ASOSIY PRODUCTION: ORACLE CLOUD
--------------------------------
Bu repository Darslik Studiyasining asosiy versiyasi. Doimiy ma'lumotlar
Oracle Cloud'dagi bitta 180 GB diskda saqlanadi. Barcha hisoblar aynan shu
fizik diskdan foydalanadi, lekin dastur bazadagi owner_id orqali ularning
video va papkalarini bir-biridan ajratadi.

  - super-admin kvotasi: 95 GB;
  - oddiy foydalanuvchi kvotasi: 10 GB;
  - faol oddiy foydalanuvchilar: ko'pi bilan 5 ta;
  - qolgan fizik joy segmentlar, audio, yakuniy video va vaqtinchalik
    natijalar uchun zaxira bo'lib qoladi.

STORAGE_DIR Oracle'dagi doimiy diskka qarashi shart. Standart o'rnatish
skripti `/opt/tarjima-storage` papkasidan foydalanadi. Agar 180 GB block
volume boshqa manzilga mount qilingan bo'lsa, STORAGE_DIR'ni aynan o'sha
mount ichidagi papkaga o'zgartiring. Source-kod papkasi yoki vaqtinchalik
diskni STORAGE_DIR sifatida ishlatmang.

ORACLE ENVIRONMENT VARIABLE'LAR
---------------------------------
systemd service ichida quyidagilarni sozlang:

    STORAGE_DIR=/opt/tarjima-storage
        Oracle'dagi 180 GB doimiy volume ichidagi haqiqiy papka.

    APP_USERNAME=<super-admin login>
    APP_PASSWORD=<super-adminning kuchli paroli>
        Birinchi ishga tushishda super-admin yaratadi. Brauzerning HTTP Basic
        Auth oynasi chiqmaydi; /login sahifasidagi forma va xavfsiz sessiya
        cookie ishlatiladi. Mavjud videolar birinchi super-adminga biriktiriladi.

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
    STORAGE_LIMIT=193273528320     (180 GB umumiy hovuz, baytlarda)
    TOTAL_STORAGE_LIMIT=193273528320 (foydalanuvchilarga ajratiladigan jami 180 GB)
    ADMIN_STORAGE_LIMIT=102005473280 (super-admin uchun 95 GB)
    USER_STORAGE_LIMIT=10737418240   (har yangi foydalanuvchi uchun 10 GB)
    MAX_REGULAR_USERS=5              (bir vaqtda faol oddiy hisoblar soni)
    REPETITION_THRESHOLD=3         (necha marta ketma-ket takrorlansa shubhali)
    UPLOAD_CHUNK_SIZE=8388608      (8 MB — faqat ma'lumot uchun, frontend o'zi belgilaydi)

ORACLE'GA JOYLASHTIRISH
-------------------------
Serverga SSH orqali kirib `scripts/setup_oracle_vm.sh` skriptini ishga
tushiring. Birinchi ishga tushirishdan oldin kuchli super-admin login va
parolini environment orqali bering. Skript repository'ni o'rnatadi, doimiy
storage papkasini tayyorlaydi va systemd xizmatini yoqadi.

180 GB volume `/opt/tarjima-storage` manziliga mount qilinganini va service
foydalanuvchisi shu papkaga yozish huquqiga ega ekanini albatta tekshiring.
Oddiy foydalanuvchilarning login/paroli keyinchalik saytning Sozlamalar ->
Ulangan odamlar bo'limidan super-admin tomonidan yaratiladi.

YANGILASH (kelajakda kod o'zgarganda)
----------------------------------------
Oracle serverida kodni yangilang va xizmatni qayta ishga tushiring:

    sudo git -C /opt/tarjima pull
    sudo systemctl restart tarjima

`/opt/tarjima-storage` ichidagi video, natija va baza kod deployidan alohida
bo'lgani uchun saqlanib qoladi.

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

Server qayta ishga tushsa (Oracle VM yoki service restart), faol (blocked_reason=None)
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

RUSCHA O'RGANISH: SO'ZLAR VA INTRO
-------------------------------------
Learning SRT'ni foydalanuvchi o'zi tayyorlaydi. So'zlar blokning VAQT
qatorida teg sifatida yoziladi (subtitr matnida teg bo'lmaydi):

    12
    00:00:10,000 --> 00:00:16,000 [yangi:че́люсть=jag‘] [takror:суста́в=bo‘g‘im]
    Pastki челюстьning движениеsi суставga bog‘liq.

  - yangi - shu videoda yangi so'z, takror - oldin o'rganilgan so'z.
    LEMMA/MA'NO ichida [ ] = : bo'lmaydi; urg'u belgisi (U+0301) va
    apostroflar mumkin. Boshqa teglar ([speed:fast]) o'zgarishsiz ishlaydi.
  - Xato (yuklash rad etiladi, blok raqami bilan): yopilmagan qavs,
    noto'g'ri formatdagi [yangi:/[takror: tegi.
  - Ogohlantirish (yuklash davom etadi): so'z blok matnida topilmadi
    (o'zak = lemma oxirgi 2 harfisiz, kamida 3 harf), bitta lemma turli
    ma'noda, bitta lemma ham yangi ham takror, yangi so'zlar 20 tadan ko'p.

Natijalar (ASOS = Learning SRT nomi, oxiridagi _LEARNING olib tashlanadi):
  - Pleyerda "So'zlar" treki (yuqori o'ng burchak, yangi - sariq,
    takror - oq) alohida yoqib-o'chiriladi: /learning/words.vtt.
  - ASOS_learning.mp4 - so'zlar kadrga yozilgan (libass, ASS fayl) va
    intro bo'lsa intro bilan yig'ilgan Learning videosi. Toza, intro'siz
    Learning videosi alohida saqlanadi.
  - ASOS_sozlar.vtt, ASOS_intro.mp4.
  - Yuqoridagi fayl nomlari RFC 5987 (filename*=) bilan beriladi.

Intro (Learning bo'limi -> "Intro yaratish"): faqat so'z teglaridan
yig'iladi - "Takrorlash: N ta so'z" (2 s), takror so'zlar 8 tadan 2x4
jadvalda (to'liq ekran 12 s, aks holda 3 + 1.2 x N, kamida 6 s, ovozsiz),
"Yangi so'zlar: N ta" (2 s), har yangi so'z kartochkasi: 0.5 s jim +
ORIGINAL + 0.6 s + O'ZBEKCHA + 0.6 s + ORIGINAL + 1.2 s jim.
  - ORIGINAL: OpenAI TTS (kirill - ruscha, lotin - inglizcha), tezlik 1.0,
    urg'u belgisi bilan; talaffuz buzilsa "Urg'u belgisisiz yuborish".
  - O'ZBEKCHA: Aisha TTS (Learning audio Aisha bilan qilingan bo'lsa shu
    kalit, aks holda formadagi kalit).
  - Audio TTS keshida saqlanadi - qayta yaratishda pul sarflanmaydi.
  - Intro Learning videosining o'lchami, fps, pikselformat, kodek va audio
    parametrlari bilan render qilinadi. Intro bilan yig'ilgan video uchun
    Learning subtitrlari va so'zlar treki (avval freeze-point, keyin intro
    uzunligi) suriladi; intro paytida subtitr ko'rinmaydi.
  - Server qayta ishga tushsa intro/eksport ishi davom ettiriladi.

Yangi bog'liqlik: Pillow. Shrift: fonts/DejaVuSans*.ttf (Bitstream Vera
litsenziyasi, fonts/LICENSE.txt). Testlar: `pip install pytest` va
`python -m pytest tests`.

TEKSHIRILGAN STSENARIYLAR
----------------------------
Ushbu versiya to'liq pipeline bo'yicha sinovdan o'tkazildi (upload -> segment
-> transcribe -> approve -> translate -> audio -> render -> completed),
shu jumladan yakuniy video yuklab olish endpointi. Server "qulashi"
simulyatsiyasi orqali qayta tiklanish tekshirildi (video_rendering
bosqichida). Haqiqiy OpenAI/Aisha API kalitlari bilan to'liq sinov
o'tkazilmadi - ishlatishdan oldin qisqa video bilan sinab ko'rishni
tavsiya qilamiz.

