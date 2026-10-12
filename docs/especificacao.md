# Especificação original do projeto (texto do Diego, sem alterações)

> Este arquivo guarda a especificação completa, como foi escrita. O resumo de trabalho
> fica em `CLAUDE.md` e o protocolo detalhado em `docs/protocolo.md`.

# Projeto: Sentinela Casa — Parte 1: aplicação do PC (Python + interface gráfica)

## Contexto
Sou o Diego, desenvolvedor Python iniciando em visão computacional. Quero um sistema de
monitoramento residencial no estilo dos softwares de NVR/CFTV, com três partes:
1. Câmeras Wi-Fi iCSee (Xiongmai) na rede local — UMA OU VÁRIAS.
2. PC (esta parte, em Python): app desktop com interface gráfica moderna que mostra as câmeras
   ao vivo, detecta e classifica objetos, grava eventos (foto + vídeo) e alerta quando algo
   fica PARADO em frente à casa.
3. Placa BitDogLab (Raspberry Pi Pico W, firmware em C, será feita depois em outro prompt):
   recebe alertas (buzzer, matriz de LED 5x5, OLED) e envia comandos do joystick para mover a câmera.

Nesta sessão desenvolva SOMENTE o app do PC + um SIMULADOR da BitDogLab em Python, para eu
testar tudo sem a placa. Explique as decisões em português, como para um iniciante.

## Regras gerais
- Python 3.11+, type hints, docstrings e comentários em português. Compatível com Windows e Linux.
- Interface gráfica com **PySide6** (Qt). Ícones com `qtawesome` (Material Design ou Phosphor).
- Nenhum IP, senha ou limite fixo no código. Ajustes gerais em `config.yaml`; câmeras e eventos em
  SQLite; senhas das câmeras guardadas com a biblioteca `keyring` (cofre de senhas do sistema
  operacional), nunca em texto puro.
- `logging` (não `print`), com arquivo de log rotativo em `pc/dados/logs/`.
- Eficiência é prioridade: a interface NUNCA pode travar. Captura, IA e gravação rodam fora da
  thread da interface; frames chegam à interface por sinais Qt.
- O app NÃO faz varredura/scan na rede procurando câmeras. Cadastrar câmeras é tarefa do usuário,
  pela interface.
- Trabalhe por FASES (lista no final). Ao terminar cada fase: rode os testes, me diga como testar
  manualmente e PARE, esperando eu confirmar antes de seguir.
- Crie `CLAUDE.md` na raiz com o resumo deste prompt, o protocolo e a fase atual.

## Estrutura de pastas (pode refinar, mantendo a separação)
```
sentinela-casa/
├── CLAUDE.md
├── README.md
├── docs/protocolo.md
└── pc/
    ├── config.yaml
    ├── requirements.txt
    ├── main.py
    ├── core/
    │   ├── modelos.py         # dataclasses: Camera, Canal, Evento, Deteccao
    │   ├── banco.py           # SQLite (câmeras, eventos)
    │   └── segredos.py        # keyring
    ├── camera/
    │   ├── stream.py          # leitura em thread, só o último frame, reconexão
    │   ├── gerenciador.py     # cria/para pipelines por câmera e canal
    │   └── ptz.py             # interface PTZ + ONVIF + Falso
    ├── vision/
    │   ├── detector.py        # YOLO compartilhado + fila de inferência
    │   ├── rastreador.py      # ByteTrack separado por câmera/canal
    │   ├── motion.py          # subtração de fundo (DESCONHECIDO e início de evento)
    │   └── dwell.py           # regra de "parado na zona"
    ├── eventos/
    │   ├── buffer.py          # buffer circular de frames (pré-evento)
    │   ├── gravador.py        # monta clipe MP4 + foto + registro no banco
    │   └── retencao.py        # apaga eventos antigos
    ├── link/
    │   └── bitdog.py          # UDP com a placa
    ├── ui/
    │   ├── tema.py            # cores, fontes, QSS
    │   ├── janela_principal.py
    │   ├── mosaico.py         # grade de câmeras
    │   ├── bloco_camera.py    # um bloco (tile) do mosaico
    │   ├── tela_camera.py     # detalhe de uma câmera
    │   ├── painel_eventos.py
    │   ├── visualizador_evento.py   # foto + player do clipe
    │   ├── dialogo_camera.py  # cadastrar/editar câmera
    │   ├── editor_zona.py
    │   └── tela_config.py
    ├── tools/
    │   ├── simulador_bitdoglab.py
    │   └── testar_camera.py   # testa UMA câmera informada (IP/usuário/senha), sem scan
    ├── dados/                 # criado em tempo de execução (fora do Git)
    │   ├── sentinela.db
    │   ├── eventos/<camera>/<AAAA-MM-DD>/
    │   └── logs/
    └── tests/
```

## Cadastro de câmeras (pela interface)
Diálogo "Adicionar câmera" (e "Editar"), com validação e visual moderno:
- **Nome** (obrigatório e ÚNICO): é o identificador da câmera em todo o sistema
  (banco, pastas de eventos, interface). Ex.: "Frente da casa".
- **Código para a placa** (obrigatório, gerado automaticamente a partir do nome e editável):
  até 10 caracteres, MAIÚSCULAS, sem acento e sem espaço (ex.: FRENTE). Único.
- **IP** na rede local (validar formato) e **porta RTSP** (padrão 554).
- **Usuário** (padrão `admin`) e **Senha** (OPCIONAL — o usuário pode ter tirado a senha
  nas configurações da câmera). Campo de senha com botão de mostrar/ocultar.
- **Quantidade de canais/lentes** (padrão 1; câmeras de duas lentes usam 2).
- **Modelo de URL RTSP** (avançado, recolhível), com variáveis {ip} {porta} {usuario} {senha}
  {canal} {stream}. Padrão iCSee:
  `rtsp://{ip}:{porta}/user={usuario}&password={senha}&channel={canal}&stream={stream}.sdp`
  (stream=0 principal, stream=1 substream). Se a senha estiver vazia, montar a URL sem ela.
  Oferecer modelos prontos em uma lista (iCSee, ONVIF genérico `/onvif1`, personalizado).
- **PTZ**: possui motor? (sim/não), porta ONVIF (padrão 8899).
- **Detecção ativada** (sim/não) e **gravar eventos** (sim/não).
- Botão **"Testar conexão"**: tenta abrir o substream e mostra uma miniatura ao vivo +
  resolução/FPS; se PTZ marcado, testa o ONVIF. Mensagens de erro claras em português
  (senha errada, IP inacessível, porta fechada, URL inválida).
- Editar e remover câmera (remover pergunta se apaga também os eventos gravados).
Renomear uma câmera atualiza o banco e a pasta de eventos.

## Interface gráfica — visual
Moderna, elegante, no estilo de softwares de monitoramento profissionais:
- Tema escuro por padrão (com opção de tema claro), fundo grafite, cartões com cantos
  arredondados, uma cor de destaque (ex.: azul-ciano), vermelho reservado para alertas.
- Fonte limpa (Inter ou Segoe UI), hierarquia clara de tamanhos, espaçamento generoso.
- Animações suaves e discretas: hover nos blocos, pulso vermelho na borda em alerta, toasts.
- Tudo responsivo ao redimensionar; tela cheia com F11.
Organize o estilo em `ui/tema.py` (paleta + QSS) para eu poder ajustar cores num lugar só.

## Tela inicial — Mosaico de câmeras
- Barra lateral esquerda com ícones: Câmeras (mosaico), Eventos (todos), Configurações.
  No rodapé da barra: status da placa BitDogLab (conectada/desconectada) e uso de CPU/GPU.
- Barra superior: título, relógio, seletor de layout (1, 2x2, 3x3, 4x4, automático),
  botão "+ Adicionar câmera".
- Um bloco 16:9 por câmera cadastrada (por canal, se tiver mais de um), mostrando o vídeo ao
  vivo do **substream**, com:
  - nome da câmera no canto inferior sobre um gradiente; ponto de status
    (verde online, amarelo conectando, cinza offline);
  - chips com as classes vistas agora (ex.: "PESSOA 1", "CARRO 2") e caixas de detecção
    opcionais (ligar/desligar nas configurações);
  - borda vermelha pulsando quando há alerta de objeto parado;
  - estado "Offline" elegante com botão "Reconectar" quando cair.
- Sem câmeras cadastradas: tela de boas-vindas com botão para cadastrar a primeira.
- Clique no bloco → abre a Tela da câmera. Duplo clique → tela cheia daquela câmera.
- Clique direito no bloco → Editar, Remover, Tirar foto, Desativar detecção.
- Toast (notificação no canto) a cada alerta, com miniatura; clicar abre o evento.

## Tela da câmera (detalhe)
- Vídeo grande do **stream principal** (resolução máxima). Se a câmera tiver mais de um
  canal/lente, mostrar todos os canais lado a lado (ou abas), cada um em alta resolução.
- Sobreposição dos objetos identificados e classificados: caixa com cor por classe e rótulo
  "PESSOA #7 0.87"; contador "parado 6/8 s" para objetos dentro da zona; zona semitransparente.
  Botão para ligar/desligar a sobreposição.
- Barra de ferramentas: voltar, tirar foto, editar zona, mostrar máscara de movimento,
  controles PTZ (setas + botão parar + preset) quando a câmera tiver motor, tela cheia.
- Faixa "Detectado agora": lista dos objetos rastreados no momento (classe, ID, confiança,
  tempo na cena).
- **Painel lateral direito de eventos** desta câmera:
  - lista em ordem do mais recente, agrupada por dia, cada item com miniatura, data e hora,
    duração, chips das classes vistas e selo "ALERTA" quando houve objeto parado;
  - filtros: dia (calendário), classe, só alertas;
  - atualiza em tempo real quando um evento novo termina.
- Clicar em um evento abre o **visualizador de evento** (no lugar do vídeo ao vivo ou em diálogo):
  foto do momento principal + player do clipe (play/pause, barra de tempo, velocidade 0.5x/1x/2x,
  quadro a quadro), com as caixas de detecção desenhadas; dados do evento (câmera, início, fim,
  classes e quantidades, maior confiança, se gerou alerta); botões "Abrir pasta", "Exportar",
  "Excluir". Botão "Voltar ao vivo".
- A aba "Eventos" da barra lateral mostra a mesma lista para TODAS as câmeras, com filtro por câmera.

## Captura de vídeo e desempenho com várias câmeras
- Cada câmera/canal tem uma thread de leitura do **substream** sempre ativa (mosaico + IA +
  gravação) e uma thread do **stream principal** que só é aberta enquanto a Tela da câmera
  estiver visível (fechar a tela encerra a leitura).
- Leitura descarta frames antigos (guarda só o mais recente) e reconecta sozinha com backoff.
- Fontes de teste no `config.yaml`, além de RTSP: `arquivo` (.mp4 em loop) e `webcam`, para eu
  cadastrar "câmeras falsas" e testar sem a câmera real.
- O mosaico atualiza no máximo a `fps_mosaico` (padrão 12); converter para QImage só os blocos
  visíveis.

## Detecção e classificação
- UM modelo Ultralytics YOLO nano compartilhado (`yolo11n.pt`, com fallback para `yolov8n.pt`),
  em uma thread de inferência que atende as câmeras em rodízio, respeitando `ia_fps` por câmera
  (padrão 6). Se houver GPU, usar lotes (batch) com os frames das câmeras.
- Rastreamento **separado por câmera/canal** com `supervision.ByteTrack` (um rastreador por
  câmera — NÃO usar `model.track(persist=True)` com várias câmeras, porque mistura os IDs).
- Classes COCO usadas (nomes sempre MAIÚSCULOS e sem acento):
  0 person→PESSOA, 1 bicycle→BICICLETA, 2 car→CARRO, 3 motorcycle→MOTO, 5 bus→ONIBUS,
  7 truck→CAMINHAO, 15 cat→GATO, 16 dog→CACHORRO.
- DESCONHECIDO: região com movimento (MOG2, área mínima configurável) onde o YOLO não detectou
  nada, ou detecção com confiança abaixo de `conf_minima` (padrão 0.40).
- Opcional por câmera: só rodar o YOLO quando houver movimento (`ia_so_com_movimento`).
- Detectar CUDA automaticamente (`half=True`); em CPU, permitir exportar para ONNX/OpenVINO
  pela tela de Configurações e usar o modelo exportado se existir.
- Converter as caixas do substream para a resolução do stream principal ao desenhar.

## Eventos (registro de movimento com foto e vídeo)
- Um **evento** começa quando há movimento relevante ou detecção de uma classe usada na câmera
  (área configurável: quadro inteiro ou só a zona) e termina após `fim_evento_s` (padrão 5) sem
  atividade. Duração máxima `max_evento_s` (padrão 60); passou disso, fecha e abre outro.
- Cada câmera mantém um **buffer circular** dos últimos `pre_evento_s` (padrão 5) segundos de frames.
- Ao terminar o evento, gravar em `dados/eventos/<camera>/<AAAA-MM-DD>/<HHMMSS>/`:
  - `clipe.mp4`: pré-evento + evento + `pos_evento_s` (padrão 3), em H.264 via PyAV
    (fallback para OpenCV `mp4v` se o H.264 não estiver disponível);
  - `foto.jpg`: o frame com o objeto de maior confiança (com e sem caixas, `foto_limpa.jpg`);
  - `deteccoes.json`: caixas por tempo, para o player desenhar as detecções.
- Gravação em thread própria, sem travar a captura. Fonte da gravação configurável:
  `substream` (padrão, mais leve) ou `principal`.
- Registro no SQLite: id, câmera, início, fim, classes e contagens, maior confiança, alerta (sim/não),
  caminhos dos arquivos.
- Retenção: apagar eventos com mais de `retencao_dias` (padrão 7) e respeitar `limite_disco_gb`.
- O player do app lê o clipe com OpenCV (não depende de codecs do sistema).

## Regra de "parado na zona" (vision/dwell.py) — com testes unitários
- Zona por câmera/canal: polígono com coordenadas normalizadas (0–1), salvo no banco.
  Editor na Tela da câmera: clicar os pontos sobre o vídeo, arrastar para ajustar, salvar/cancelar.
  Sem zona definida = quadro inteiro.
- Para cada ID rastreado com centro dentro da zona, guardar histórico de posições.
- Parado = deslocamento máximo do centro < `limite_movimento` (padrão 5% da largura)
  durante `tempo_parado[classe]` segundos (padrões: PESSOA 8, MOTO 15, CARRO 30,
  BICICLETA 10, DESCONHECIDO 15, GATO/CACHORRO 20, ONIBUS/CAMINHAO 30).
- Nível por classe: PESSOA=ALTO; CARRO, MOTO, CAMINHAO, ONIBUS, DESCONHECIDO=MEDIO;
  GATO, CACHORRO, BICICLETA=BAIXO. Valores ajustáveis por câmera na tela de Configurações.
- Alertar UMA vez por ID; cooldown por ID (`cooldown_s`, padrão 120).
- Objetos já parados nos primeiros `aquecimento_s` (padrão 20) após a câmera conectar NÃO
  geram alerta (ex.: carro do vizinho estacionado).
- Tolerar o ID sumir por até 2 s (oclusão) sem zerar o contador.
- Alerta → marca o evento atual como ALERTA, toast na interface, borda vermelha no bloco e
  mensagem para a placa. Objeto alertado sai da zona ou some por mais de 5 s → LIMPAR.
- Objeto que atravessa a zona sem parar → PASSOU (no máximo 1 por ID).
- Testes pytest simulando trilhas de posições (sem vídeo).

## PTZ (camera/ptz.py)
- Interface `ControladorPTZ`: `mover(x: float, y: float)`, `parar()`, `ir_preset(n)`.
- `PTZOnvif` com `onvif-zeep` (porta do cadastro), `ContinuousMove` proporcional ao joystick e `Stop`.
- `PTZFalso`: registra no log e mostra um indicador de direção na tela (para testes).
- Falha no ONVIF → cai para `PTZFalso` com aviso claro.
- Segurança: sem comando JOY por 3 s com a câmera em movimento → `parar()`. No máximo 10 comandos/s.
- Câmera controlada pelo joystick: a que está aberta na Tela da câmera; no mosaico, a última
  selecionada. O botão J da placa alterna entre as câmeras com PTZ.
- (Opcional, só a interface) `PTZDvrip` com `python-dvr` para câmeras sem ONVIF.

## Comunicação com a BitDogLab — PROTOCOLO FIXO (não alterar)
Escreva em `docs/protocolo.md`. O firmware em C será escrito depois a partir dele.
- UDP sobre Wi-Fi. PC escuta na porta 5005; placa escuta na porta 5006.
- Uma mensagem por datagrama, texto ASCII sem acentos, campos separados por `;`, terminando
  em `\n`, no máximo 64 bytes. Decimais com ponto.
- `<CAM>` = código da câmera para a placa (até 10 caracteres, cadastro).
- IP da placa nas Configurações; o PC também aprende o endereço pelo último pacote recebido.

PC → Placa:
| Mensagem | Exemplo | Significado |
|---|---|---|
| `ALERTA;<CAM>;<CLASSE>;<seg>;<conf>;<id>;<NIVEL>` | `ALERTA;FRENTE;PESSOA;12;0.87;7;ALTO` | objeto parado na zona |
| `LIMPAR;<CAM>;<id>` | `LIMPAR;FRENTE;7` | objeto alertado saiu |
| `PASSOU;<CAM>;<CLASSE>` | `PASSOU;FRENTE;CARRO` | passou sem parar (informativo) |
| `CAM;<CAM>` | `CAM;FRENTE` | câmera que o joystick controla agora |
| `PING;<seq>` | `PING;42` | a cada 2 s |
| `HORA;<HH:MM>` | `HORA;21:35` | a cada 60 s e ao conectar |

Placa → PC:
| Mensagem | Exemplo | Significado |
|---|---|---|
| `JOY;<x>;<y>` | `JOY;-0.80;0.00` | joystick de -1.00 a 1.00 (só quando muda, até 20/s) |
| `JOY;0.00;0.00` | | joystick solto → parar câmera |
| `BTN;<A\|B\|J>` | `BTN;J` | A = silenciar alerta, B = trocar modo, J = próxima câmera com PTZ |
| `MODO;<NORMAL\|NOITE>` | `MODO;NOITE` | modo atual da placa (informativo) |
| `PONG;<seq>` | `PONG;42` | resposta ao PING |
| `OLA` | `OLA` | placa ligou/conectou |

Regras:
- 6 s sem PONG → placa "desconectada" (mostrar na barra lateral).
- Ao receber `OLA`: reenviar `HORA`, `CAM` e o alerta ativo, se houver.
- `BTN;A` → alerta atual silenciado (não reenviar); `BTN;J` → trocar câmera e responder `CAM`.
- Vários alertas ao mesmo tempo: enviar o de maior nível (empate: o mais recente).
- Mensagens inválidas são ignoradas e registradas no log, nunca derrubam o app.

## Simulador da BitDogLab (tools/simulador_bitdoglab.py)
Programa separado em Tkinter (sem dependências extras), que se comporta EXATAMENTE como a placa:
- Escuta UDP na 5006 e envia ao PC na 5005 (IP do PC por argumento, padrão 127.0.0.1).
- Envia `OLA` ao iniciar e responde `PONG` a cada `PING`.
- "OLED": área preta 128x64 ampliada 4x, texto branco. Repouso: "MONITORANDO", hora, câmera do
  joystick e último PASSOU. Alerta: código da câmera, classe em destaque e "PARADO 12s".
- "Matriz 5x5": 25 quadrados com um ícone por classe; vermelho piscando=ALTO, amarelo=MEDIO,
  verde=BAIXO, azul=DESCONHECIDO.
- "Buzzer": bell do sistema com padrão por nível (ALTO 3 bipes rápidos, MEDIO 1 longo,
  BAIXO nenhum) + indicador "BUZZER ON". Modo NOITE = sem som.
- "Joystick": setas/WASD e um pad arrastável com o mouse gerando `JOY;x;y`, zona morta,
  envio só quando muda; soltar envia `JOY;0.00;0.00`.
- Botões na tela e teclas A, B, J.
- Painel de log com as mensagens enviadas e recebidas.

## Configurações (tela)
Gerais: tema, layout do mosaico, `fps_mosaico`, mostrar caixas no mosaico, modelo YOLO e
dispositivo (CPU/CUDA), exportar modelo, retenção e limite de disco, pasta de dados.
Placa: IP, portas, status. Por câmera: detecção, gravação, área do evento, tempos de parado,
níveis, cooldown. Salvar sem reiniciar o app sempre que possível.

## Ferramenta de diagnóstico (tools/testar_camera.py)
Testa UMA câmera informada por argumentos (`--ip --usuario --senha --canal`), sem scan de rede:
portas 554, 8899 e 34567; abre substream e stream principal e informa resolução e FPS;
tenta ONVIF e lista perfis e suporte a PTZ. Saída clara em português.
(A mesma lógica é reutilizada pelo botão "Testar conexão" do cadastro.)

## Fases (pare ao final de cada uma)
0. Estrutura, `CLAUDE.md`, `requirements.txt`, `config.yaml`, `docs/protocolo.md`, banco SQLite,
   `tools/testar_camera.py`.
   Pronto quando: `python tools/testar_camera.py --ip ...` mostra o diagnóstico.
1. Janela principal com tema, barra lateral, cadastro/edição/remoção de câmeras (com keyring e
   "Testar conexão") e mosaico ao vivo (substream; também fontes arquivo/webcam).
   Pronto quando: cadastro 2 câmeras (pode ser 1 real + 1 vídeo) e as duas aparecem ao vivo, fluidas.
2. Tela da câmera com stream principal, múltiplos canais e tela cheia.
   Pronto quando: ao abrir uma câmera vejo a imagem em resolução máxima; ao voltar, o stream
   principal é encerrado.
3. Detector compartilhado + ByteTrack por câmera + DESCONHECIDO + sobreposições + "Detectado agora".
   Pronto quando: as classes aparecem rotuladas nas duas câmeras e a interface continua fluida.
4. Eventos: buffer, gravação de clipe/foto/JSON, banco, painel lateral, aba Eventos,
   visualizador com player, retenção.
   Pronto quando: um movimento gera um evento na lista com data e hora, e ao clicar vejo a foto
   e o vídeo com as caixas.
5. Editor de zona + `dwell.py` com testes + alertas (toast, borda vermelha, selo no evento).
   Pronto quando: os testes passam e alguém parado 8 s na zona gera um único alerta.
6. `link/bitdog.py` + simulador da BitDogLab.
   Pronto quando: alertas aparecem no simulador com o código da câmera e o status da placa
   aparece na barra lateral.
7. PTZ (Falso e ONVIF) pelos botões da tela, pelas setas do teclado e pelo joystick do simulador;
   botão J alterna a câmera.
   Pronto quando: com PTZFalso o indicador acompanha o joystick; com ONVIF a câmera gira e para.
8. Tela de Configurações completa + README com instalação, uso e capturas de tela.

Comece pela Fase 0.
