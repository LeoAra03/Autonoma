# arena-web

Asistente que usa arena.ai **en tu propio Brave**, por CDP, sin tocar tu ratón ni
tu teclado. Tú abres la ventana, inicias sesión y te vas a ver una película; el
bot manda un turno, espera a que responda, manda el siguiente.

Se construye por etapas, cada una corre sola:

| Etapa | Qué hace | Estado |
|-------|----------|--------|
| **1** | Conecta por CDP y **diagnostica** el DOM (`./execute --probe`). No envía ni un prompt. | ✅ esta |
| 2 | Envía **un** turno y detecta el fin de la respuesta. | pendiente |
| 3 | El lazo completo con `--hasta-que-pare`, ritmo humano y logs. | pendiente |
| 4 | `critic.py`: revisa el resultado antes de dar el turno por bueno. | pendiente |

---

## 1. Requisitos

- Python 3.10 o más nuevo.
- Playwright (sólo la librería; **no** hace falta `playwright install`: nos
  adjuntamos a tu Brave, no descargamos ningún navegador):

```bash
cd arena-web
pip install -r requirements.txt
```

## 2. Arranca Brave una vez y déjalo vivo

El bot no abre navegadores: se engancha al que ya tienes. Esa copia usa un
perfil aparte (`.brave-autonoma`) para no mezclarse con tu navegación normal, y
lleva los flags que evitan que Brave congele la pestaña cuando no está al frente
(sin ellos, una pestaña tapada se duerme y el bot se queda esperando).

**Windows** (atajo, o en PowerShell):

```bat
"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" ^
  --remote-debugging-port=9222 --remote-allow-origins=* ^
  --user-data-dir="%USERPROFILE%\.brave-autonoma" ^
  --disable-background-timer-throttling --disable-renderer-backgrounding ^
  --disable-backgrounding-occluded-windows ^
  --disable-features=CalculateNativeWinOcclusion https://arena.ai/agent
```

**Linux:**

```bash
brave-browser --remote-debugging-port=9222 \
  --remote-allow-origins='*' --user-data-dir="$HOME/.brave-autonoma" \
  --disable-background-timer-throttling --disable-renderer-backgrounding \
  --disable-backgrounding-occluded-windows \
  --disable-features=CalculateNativeWinOcclusion https://arena.ai/agent
```

**macOS:**

```bash
"/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" \
  --remote-debugging-port=9222 --remote-allow-origins='*' \
  --user-data-dir="$HOME/.brave-autonoma" \
  --disable-background-timer-throttling --disable-renderer-backgrounding \
  --disable-backgrounding-occluded-windows \
  --disable-features=CalculateNativeWinOcclusion https://arena.ai/agent
```

En esa ventana: **inicia sesión y conecta GitHub**. Ya está. Esa pestaña es la
del bot; puedes taparla con lo que quieras.

> Si quieres comprobar que el puerto quedó abierto: <http://127.0.0.1:9222/json/version>
> debe devolver un JSON con `"Browser"`.

## 3. El sondeo (Etapa 1)

```bash
./execute --probe          # Windows: execute.bat --probe
./execute --probe --json   # para máquinas
./execute --probe --sin-inventario
```

Sale algo así:

```
SELECTORES
GRUPO                ESTRATEGIA                                RESUELVE  N   VIS
----------------------------------------------------------------------------------
caja_prompt          role=textbox name="Ask"                   sí        1   sí
                     placeholder="Ask"                         no        0
boton_enviar         role=button name="Send"                   no        0
                     aria-label="Send message"                 no        0

CANDIDATOS REALES (para corregir selectors.toml sin tocar Python)
TAG       ROLE          VIS  NOMBRE
----------------------------------------------------------------------------------
button                  sí   New chat
textarea  textbox       sí   Ask anything
button                  sí   Enviar ahora
```

La lectura es simple: **lo que dice `no` en `RESUELVE` no sirve**, y en
`CANDIDATOS REALES` está el nombre o rol que sí existe en tu pantalla. Ahí
arriba el botón de enviar se llama «Enviar ahora», no «Send»: se corrige
añadiendo una línea al TOML, sin tocar Python.

Cada sondeo queda anotado en `logs/probe.jsonl`.

## 4. Cómo se escribe `selectors.toml`

```toml
[boton_enviar]
descripcion = "botón de enviar"
estrategias = [
  { kind = "role", role = "button", name = "Enviar ahora" },   # ← la tuya, primero
  { kind = "role", role = "button", name = "Send" },           # alternativas
  { kind = "aria", label = "Send message" },
  { kind = "text", text = "Enviar" },
]
```

Se prueban **en orden** y gana la primera que funcione, así que pon primero la
que te funcionó a ti. Los `kind` disponibles (todos por rol, etiqueta, texto o
placeholder; **nunca clases**):

| `kind` | Busca por | Claves |
|--------|-----------|--------|
| `role` | rol ARIA, opcionalmente con nombre | `role`, `name` |
| `aria` | etiqueta ARIA | `label` |
| `placeholder` | texto de fondo del campo | `placeholder` |
| `text` | texto visible | `text` |
| `testid` | `data-testid` | `id` |
| `css` | selector CSS estructural (**último recurso**: sólo `textarea`, `main`, `[contenteditable='true']`…) | `selector` |

Un selector mal escrito no tumba nada: sale como `error` en la tabla y se sigue
con la siguiente alternativa.

## 5. Ritmo y límites

Van en `config.toml`, y en la Etapa 3 el código los aplica sin que nadie los
recuerde:

- **Espera entre turnos:** entre 5 y 15 segundos, con jitter.
- **Una sola pestaña.** Nada de abrir conversaciones en paralelo.
- **Pausa larga:** 90 segundos cada 8 turnos.
- **Un turno a la vez:** no se manda el siguiente hasta que el anterior termina.
- `--max N`: tope duro de turnos.
- **Timeout por turno:** 15 min; si arena.ai no termina, se anota y se para.
- Al parar, **arena.ai queda intacta** con su historial; sólo se cierra la
  conexión nuestra.

## 6. Nota legal, para que conste

Arena.ai no tiene API pública para esto, así que la única forma de automatizarla
es por la interfaz, y sus [Términos de Uso](https://help.arena.ai/articles/5629909088-terms-of-use)
(actualizados el 2026-02-23) dicen en su sección 5 que el usuario se compromete a
no:

> *(vi) access the Services through programmatic or automated means or automatically
> query the Services*

…y a no usar *spiders, robots, scrapers, crawlers* ni herramientas de minería
para extraer datos (vii). La sección 6(e) permite además suspender el acceso
*por cualquier motivo o sin motivo*.

Lo que este asistente hace y lo que no:

- **Sí:** usar la cuenta de quien lo corre, para su trabajo personal o interno
  (sección 1), **un turno a la vez**, con ritmo humano.
- **No:** explotación comercial (5.i), manipular votos o rankings (5.ii), ni
  scraping masivo (5.vii). No hay votos, no hay extracción, no hay reventa.
- **Riesgo real:** la cláusula 5(x) sobre inundar el servicio. Por eso el ritmo
  está **en el código**, no en un comentario: esperas de 5–15 s, pausas largas,
  `--max N` y una sola pestaña.

Nada de esto evade controles técnicos ni oculta lo que es: es un script que usa
la web como la usarías tú, más lento. Si un día arena.ai publica una API, hay
que pasarse a ella. Y si te llega un aviso, paras: la cuenta es tuya, el código
no se va a pelear con ellos.

## 7. Pruebas

```bash
python -m pytest arena-web/tests -q    # desde la raíz del repo
cd arena-web && python -m pytest tests -q
cd arena-web && ruff check .           # extiende la configuración del agente
cd arena-web && mypy                   # strict, igual que el agente
```

No necesitan navegador: el sondeo se prueba contra una página falsa, porque la
lógica es nuestra y el DOM es de arena.ai.

## 8. Lo que **no** hace

- No controla tu ratón ni tu teclado (nada de `pyautogui`/`xdotool`): todo va por
  CDP a una pestaña, así que puedes seguir usando el PC.
- No te rinde un examen. Puedes dejarlo trabajando en **otra** cosa mientras tú
  rindes el tuyo; rellenar respuestas por ti, no.
- No expone tu PC a internet: el puerto 9222 es local.
