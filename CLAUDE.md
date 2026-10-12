# Sentinela Casa — guia do projeto

NVR/CFTV caseiro: câmeras Wi-Fi iCSee (Xiongmai) → app desktop no PC (Python/PySide6) que
mostra ao vivo, detecta/classifica objetos (YOLO), grava eventos (foto + vídeo) e alerta quando
algo fica PARADO em frente à casa → placa BitDogLab (Pico W, firmware em C, **outro projeto**,
feito depois) recebe alertas por UDP e envia o joystick para mover a câmera (PTZ).

Usuário: Diego, dev Python iniciante em visão computacional. **Explicar decisões em português,
de forma didática.** Escopo desta parte: só o app do PC + simulador da placa em Python.

- Especificação completa e original: [docs/especificacao.md](docs/especificacao.md) — **releia a
  seção da fase antes de começá-la**.
- Protocolo UDP (fixo): [docs/protocolo.md](docs/protocolo.md).

## Fase atual

**Fase 10 — firmware da placa + ajustes pedidos pelo Diego (12/10, aguardando o teste real dele).**
Firmware em `bitDogLab/` (`bitDogLab.c` → `main.c`; o `lwipopts.h` MANTÉM o nome) gravado na placa real
(IP 192.168.0.104; o Diego preencheu o Wi-Fi). Rodada de 12/10 (pedido longo do Diego): filtro de objetos
(`ia.identificar`) e alerta por classe (`parado.alertar`); lente sem zona não alerta (`parado.sem_zona`);
objetos "fantasmas" com "só analisar com movimento" (repetição + `ia.verificar_parado_s`); suavização do
vídeo (`interface.suavizar_ms`); botão A 1 toque/2 toques e buzzer contínuo (decisão 5); manual da placa
em Ajustes → Placa. Ponta a ponta com a placa real pela rede OK (sem buzzer: alerta BAIXO). Falta o teste
dele (na frente da câmera, botões, joystick).
Fase 9 concluída: correção de MEMÓRIA confirmada pelo Diego (12/10, 10:20, depois de aumentar a paginação);
correção do viés na prova do treino aprovada e aplicada em 12/10 (a medição de épocas foi interrompida:
só saiu 10 épocas = 0,790 x 0,830 do yolo11s; `treino.epocas` ficou 30).
(Fase 7 aprovada em 12/10/2026: o motor real girou e parou; Fase 8 aprovada em 12/10/2026.)
(Fase 0 aprovada em 11/10/2026, junto com: dividir lentes, esclarecimentos E1–E11 do protocolo,
Python 3.14, canais a partir de 1. Fases 1 a 5 aprovadas em 11/10/2026 com a câmera real; Fase 6
aprovada em 12/10/2026 com o simulador num notebook — PC 192.168.1.101, notebook 192.168.1.102.)

| Fase | Conteúdo | Pronto quando |
|---|---|---|
| 0 ✅ | estrutura, CLAUDE.md, requirements, config.yaml, protocolo, banco SQLite, `tools/testar_camera.py` | `python tools/testar_camera.py --ip ...` mostra o diagnóstico |
| 1 ✅ | janela (tema, barra lateral), cadastro/edição/remoção de câmeras (keyring, "Testar conexão"), mosaico ao vivo do substream (+ fontes arquivo/webcam) | 2 câmeras (1 real + 1 vídeo) ao vivo, fluidas |
| 2 ✅ | Tela da câmera: stream principal, múltiplos canais, tela cheia | resolução máxima; ao voltar, o principal é encerrado |
| 3 ✅ | YOLO compartilhado + ByteTrack por câmera + DESCONHECIDO + sobreposições + "Detectado agora" | classes rotuladas nas 2 câmeras, UI fluida |
| 4 ✅ | eventos: buffer, clipe/foto/JSON, banco, painel lateral, aba Eventos, player, retenção **+ coletor de imagens "difíceis" para a Fase 9** | movimento → evento na lista; foto e vídeo com caixas |
| 5 ✅ | editor de zona + `dwell.py` com testes + alertas (toast, borda, selo) | testes passam; parado 8 s → 1 alerta |
| 6 ✅ | `link/bitdog.py` + simulador Tkinter | alertas no simulador; status da placa na barra |
| 7 ✅ | PTZ (Falso/ONVIF) por botões, teclado e joystick; J alterna câmera | indicador acompanha joystick; câmera ONVIF gira e para |
| 8 ✅ | Tela de Configurações completa + README com capturas | ajustes valem sem reiniciar; README com capturas |
| 10 | **firmware da placa** (`bitDogLab/`, C + Pico SDK 2.3.1): Wi-Fi, UDP, OLED, matriz, buzzer, botões, joystick; lógica igual à do simulador | alertas na placa real; joystick move a câmera; A/B/J funcionam |
| 9 ✅ | **(nova, pedida pelo Diego)** aprendizado contínuo (ver decisão 3): tela de revisão das imagens coletadas (caixas pré-preenchidas, corrigir/aceitar), treino do YOLO na GPU, comparação antes/depois nas imagens dele, trocar/voltar modelo com 1 clique | modelo novo só é usado se for melhor nas imagens de validação dele |

**Regra de trabalho:** ao fim de cada fase, rodar os testes, explicar como testar manualmente e
**PARAR** esperando o Diego confirmar. Atualizar esta seção ao concluir cada fase.

## Decisões que MUDAM a especificação original (aprovadas pelo Diego — não reverter)

1. **Lentes empilhadas** (11/10): câmeras com 2 lentes numa imagem são cortadas em vistas (ver abaixo).
2. **IA na imagem HD** (11/10): `ia.fonte: principal` — a IA analisa o stream principal (2304x1296
   por lente, YOLO em `ia.tamanho_imagem_hd` = 1280) para enxergar objetos distantes; o HD das
   câmeras com detecção fica SEMPRE aberto (dono "ia"); se o HD cair, a IA usa o substream.
   Com isso, sair da Tela da câmera só fecha o HD se a IA não estiver usando (`fechar_principal`
   por dono). `ia.fonte: substream` volta ao comportamento original. Custo medido: ~6% de CPU.
3. **Fase 9 — treinamento com os dados das câmeras** (11/10): aprendizado ATIVO com revisão humana
   (auto-treino com as próprias respostas reforçaria os erros: o que o modelo não vê nunca vira
   exemplo). A coleta começa na Fase 4: guardar quadros "difíceis" (confiança entre
   `conf_deteccao` e ~0,6, troca de classe/ID, DESCONHECIDO, objeto perdido) + amostras aleatórias
   de dia e de noite, com pré-rótulos no formato YOLO, em `dados/treino/`. Nunca trocar o modelo
   automaticamente; validar nas imagens dele e manter o anterior para voltar.
   **Atualizado em 12/10 a pedido do Diego ("aprender de forma contínua e automática"):** o ciclo
   passa a ser AUTOMÁTICO, mas sem aprender com as próprias respostas: um modelo PROFESSOR grande
   (YOLO11x, só durante o treino) rotula as imagens coletadas; imagem com qualquer detecção duvidosa
   do professor vai para a revisão (opcional) e não entra no treino; treino de madrugada quando há
   imagens novas suficientes; o modelo novo só substitui o atual se for MELHOR na validação
   (separada por blocos de hora, nunca usada no treino) e nenhuma classe piorar além do limite;
   histórico com "voltar" em 1 clique.
4. **Placa ⇄ PC: o PC puxa a conversa** (11/10, pedido do Diego): a placa mostra no OLED
   "AGUARDANDO PC" + o IP e a porta dela; a pessoa cadastra esse IP no app (Ajustes → Placa
   BitDogLab, `placa.ip`); o PC manda PING e a placa responde ao endereço de ORIGEM (IP **e porta**).
   A placa não precisa do IP do PC no firmware e funciona através do roteador 2 (NAT) do Diego.
   E7/E8 reescritos e E12 criado em docs/protocolo.md — os FORMATOS das mensagens não mudaram.
5. **Botão A e buzzer da placa** (12/10, pedido do Diego): **1 toque** no A = mudo liga/desliga (só na
   placa); **2 toques** (até 0,4 s) = cala o alerta ATUAL + `BTN;A`, e o PC CONTINUA reenviando o alerta
   (antes parava e ele sumia da placa em 6 s; `_silenciados` saiu do `link/bitdog.py`). Mensagem NOVA
   `SOM;<UMA|CONTINUO>` (E13, `placa.buzzer` em Ajustes → Placa; ao conectar, no OLA e ao mudar):
   CONTINUO repete o padrão (ALTO 1,5 s, MÉDIO 2,5 s) até o alerta acabar. Única adição de formato ao
   protocolo; placas antigas ignoram. E4/E10/E14 e a regra 3 reescritos em docs/protocolo.md.

## Regras gerais (da especificação)

- Python 3.11+ (venv atual: **3.14** em `.venv/` na raiz), type hints, docstrings e comentários
  em português. Windows e Linux.
- GUI **PySide6** (pacote `PySide6-Essentials`), ícones **qtawesome** (Material Design/Phosphor).
- **Nada de IP, senha ou limite fixo no código.** Ajustes em `pc/config.yaml`; câmeras/zonas/eventos
  no SQLite; senhas no **keyring** (nunca em texto puro). Constantes de *formato/protocolo*
  (ex.: código ≤ 10 caracteres) podem ficar no código.
- `logging` (não `print`), arquivo rotativo em `pc/dados/logs/`. **Nunca logar URL sem
  `mascarar_url()`**.
- A interface NUNCA trava: captura, IA e gravação fora da thread da UI; frames chegam por sinais Qt.
- **Sem varredura/scan de rede.** Câmeras são cadastradas pelo usuário.
- Nomes de classes sempre MAIÚSCULOS e sem acento: PESSOA, BICICLETA, CARRO, MOTO, ONIBUS,
  CAMINHAO, GATO, CACHORRO, DESCONHECIDO (mapa COCO em `core/modelos.py`).
- Rastreamento: um `supervision.ByteTrack` **por câmera/canal** (nunca `model.track(persist=True)`).

## Como rodar

```bash
# na raiz do projeto
python -m venv .venv
.venv\Scripts\activate            # Linux: source .venv/bin/activate
pip install -r pc/requirements.txt
cd pc
python -m pytest                  # testes
python main.py                    # o app
python tools/testar_camera.py --ip 192.168.0.50 --pedir-senha
python tools/gerar_video_teste.py [--duas-lentes | --pessoas]   # vídeos de teste (pc/videos/)
python tools/simulador_bitdoglab.py              # placa simulada (mostra o IP; cadastre no app)
```

## Estrutura (`pc/`)

| Caminho | Papel |
|---|---|
| `config.yaml` | ajustes gerais (ruamel.yaml, preserva comentários ao salvar) |
| `core/config.py` | `Config.carregar()`, `obter("a.b")` (erro claro se faltar), `definir`, `salvar` atômico |
| `core/logs.py` | `configurar_logs()` (arquivo rotativo + console) |
| `core/modelos.py` | dataclasses `Camera`, `Vista`, `Evento`, `Deteccao`; enums `TipoFonte`, `TipoStream`, `DisposicaoLentes`, `Nivel`; `CLASSES_COCO` |
| `core/url_rtsp.py` | `montar_url()` (senha vazia → sem senha), `mascarar_url()` |
| `core/validacao.py` | `validar_*` → mensagem PT ou `None`; `gerar_codigo()` |
| `core/banco.py` | `Banco` (SQLite, WAL, RLock, migrações por `PRAGMA user_version`; esquema v2) |
| `core/servico_cameras.py` | cadastrar/editar/renomear/remover/foto: banco + cofre + pastas em sincronia |
| `core/segredos.py` | senhas no keyring: chave `camera-<id>`, serviço `sentinela-casa` |
| `camera/captura.py` | `configurar_opencv()`, `definir_variavel_ffmpeg()`, `abrir_captura(fonte, abertura_s, leitura_s)`, `salvar_jpg()` |
| `camera/rede.py` | `testar_porta()` (uma porta de um IP) |
| `camera/sonda_rtsp.py` | DESCRIBE manual → 401/404/503 explicados; Basic/Digest |
| `camera/diagnostico.py` | `diagnosticar()`, `testar_onvif()` — também usados pelo "Testar conexão" |
| `camera/stream.py` | `LeitorVideo`: thread por fonte, só o último quadro, backoff (`calcular_espera`), arquivo em loop no ritmo do vídeo |
| `camera/gerenciador.py` | `GerenciadorCameras`: 1 leitor de SUBSTREAM por câmera x canal (`sincronizar`); principal só com a Tela da câmera (`abrir_principal`/`fechar_principal`); `melhor_leitor(vista)` = principal se já tem imagem, senão substream |
| `ui/tema.py` | paletas `ESCURO`/`CLARO`, QSS, `icone()`, `cor()`, sinal `tema_mudou` |
| `ui/distribuidor.py` | `DistribuidorQuadros(obter_leitor, fps, obter_resultado, obter_parado)` (QThread): recorta lente, redimensiona e cria QImage só dos blocos visíveis; emite também `deteccoes_prontas` e `parado_pronto` (SituacaoParado, quando a `versao` muda). Mosaico usa `leitor_da_vista`; Tela da câmera usa `melhor_leitor` |
| `ui/tela_camera.py` | Tela da câmera: abas Todas/vistas, selo HD/carregando, foto em alta resolução, tela cheia; painel de eventos (botão "Eventos") e visualizador no lugar do vídeo (`mostrar_evento`/`voltar_ao_vivo`); botão "Zona" (`editar_zona`/`salvar_zona`/`cancelar_zona`, barra troca para Desfazer/Limpar/Cancelar/Salvar, sinal `zona_salva`) |
| `ui/pagina_ajustes.py` | `PaginaAjustes`: seções (Aparência, IA, Gravação, Alertas, Placa, PTZ, Câmeras, Sistema), barra "Alterações não salvas" (Descartar/Salvar), `salvar()` grava config.yaml e/ou `servico.salvar_ajustes` e emite `salvo(chaves, cameras)`; `editor(chave)`/`editor_camera(chave)` |
| `ui/formulario.py` | `Campo` (chave, tipo, faixa, sufixo, `escala` p/ %), `EditorAjuste` (valor/erro/alterado/confirmar/descartar; modo "por câmera": vazio = padrão), `TabelaClasses` (tempo parado + nível por classe) |
| `ui/ajustes_placa.py` | `SecaoPlaca` (Ajustes → Placa BitDogLab): IP/portas, valida (`validar_ip_placa`, `validar_portas_placa`), grava no config.yaml e chama `link.reconfigurar` (sem reiniciar); status ao vivo (`texto_status`) |
| `ui/controles_ptz.py` | `ControlesPTZ` (painel flutuante: setas segurar/soltar com repetição a cada 1 s, parar, "Posições" ir/salvar 1–4, aviso) e `IndicadorPTZ` (bolinha = velocidade pedida) |
| `ui/editor_zona.py` | `EditorZona`: camada sobre o bloco; clique cria ponto (`indice_insercao`), arrastar move, botão direito apaga, desfazer; escurece o que fica fora da zona |
| `ui/painel_eventos.py` | `PainelEventos` (lista por dia com miniatura, chips, selo ALERTA; filtros câmera/classe/só alertas/dia; `evento_gravado`/`eventos_removidos` em tempo real), `ModeloEventos`, `DelegadoEventos` |
| `ui/visualizador_evento.py` | `VisualizadorEvento`: player (play/pausa, linha do tempo, 0,5/1/2x, quadro a quadro, caixas do `deteccoes.json`), foto, dados, Abrir pasta/Exportar/Excluir |
| `ui/reprodutor.py` | `Reprodutor`: lê o clipe com OpenCV numa QThread própria (pedidos por sinais `pedir_*`); `fechar_e_esperar()` solta o arquivo antes de apagar |
| `ui/pagina_eventos.py` | aba Eventos: painel de todas as câmeras + visualizador |
| `ui/grade.py` | matemática da grade 16:9 (sem Qt) |
| `ui/mosaico.py`, `ui/bloco_camera.py` | mosaico (páginas, solo) e bloco desenhado à mão (status, offline, hover; zona tracejada, rótulo "parado 6/8 s", caixa vermelha "PARADO 12 s", borda vermelha pulsando + selo "ALERTA"; `area_imagem` para o editor) |
| `ui/janela_principal.py` | janela, barra superior, menus, foto, F11; clique no bloco abre a Tela da câmera; `_PonteEventos`/`_PonteAlertas` levam os avisos dos serviços para a thread da UI; toast de ALERTA (`abrir_alerta`: evento gravado ou câmera ao vivo) |
| `ui/barra_lateral.py` | navegação + placa + CPU/GPU (`pynvml`) |
| `ui/dialogo_camera.py` | cadastro/edição com validação e "Testar conexão" (prévia ao vivo + sugestão de lentes) |
| `ui/toast.py`, `ui/componentes.py`, `ui/tarefas.py` | notificações; Interruptor/CampoSenha/CampoNumero...; `executar()` em segundo plano |
| `tools/testar_camera.py` | CLI de diagnóstico (saída 0 = substream OK) |
| `tools/gerar_video_teste.py` | vídeos sintéticos (`--duas-lentes` = 640x720 como a XM232) |
| `vision/motion.py` | `DetectorMovimento` (MOG2 numa imagem reduzida, aquecimento, regiões normalizadas, máscara) |
| `vision/rastreador.py` | `RastreadorVista` (um `sv.ByteTrack` POR VISTA) + regra do DESCONHECIDO + classe estável por ID |
| `vision/detector.py` | `ModeloYolo`, `carregar_modelo` (GPU auto, half, reserva yolov8n, exportado na CPU), `exportar_modelo`, `ServicoDeteccao` (thread, rodízio `ia_fps`, lote, `resultado_da_vista`, `assinar`) |
| `camera/ptz.py` | `ControladorPTZ`; `PTZOnvif` (`conectar` acha o perfil com PTZ; ContinuousMove/Stop; presets pelo NOME "Sentinela N"); `PTZFalso`; `ServicoPTZ` (thread "ptz": `mover`/`parar_camera`/`parar_outras`/`ir_preset`/`salvar_preset`/`estado` → `EstadoPTZ`, `assinar_vista_movida`) |
| `treino/dados.py` | `ConjuntoTreino` (pastas `pendentes`/`rotuladas`/`revisar`, `separar` treino x validação por blocos de 1 h com o lado GRAVADO no .json, `escrever_listas` p/ o Ultralytics, `aplicar_limite`), `Amostra` (.jpg + .txt YOLO com ids COCO + .json) |
| `treino/rotulador.py` | professor: `decidir` (certeza → rotuladas; dúvida → revisar, já preenchida; vazia também serve), `rotular_pendentes` |
| `treino/avaliacao.py` | `Metricas` (mAP50-95, mAP50, por classe), `metricas_do_ultralytics`, `decidir_promocao` (margem + queda máxima por classe) |
| `treino/historico.py` | `Rodada` e `Historico` (`dados/treino/historico.json`, gravação atômica) |
| `treino/memoria.py` | `medir()` → `Memoria` (RAM livre e memória RESERVÁVEL livre = commit no Windows), `falta_para` (portão de início), `orcamento_placa` (quanto o treino pode reservar na placa), `eh_falta_de_memoria`, `MemoriaInsuficiente` |
| `treino/ciclo.py` | uma rodada (processo à parte: `python -m treino.ciclo --config ... [--forcar]`): professor → limite → listas → nota atual → treino (a partir do modelo atual, `freeze`, `val=False`, `last.pt`) → nota nova → copia `sentinela_AAAAMMDD_HHMM.pt` se aprovado; `Pecas` trocáveis nos testes; avisos = linhas JSON na saída |
| `treino/servico.py` | `ServicoTreino`: agenda (1x/dia após `treino.horario`, `deve_rodar`, `agenda.json`), dispara o processo (prioridade baixa, sem janela), lê os avisos, `treinar_agora`, `cancelar` (taskkill /T) |
| `ui/pagina_treino.py` | página "Treino": cartões (pendentes, prontas, revisar, modelo em uso), "aprender sozinho", "Treinar agora", progresso, histórico com "Usar este" |
| `ui/revisor.py` | `DialogoRevisao` + `TelaRotulos`: aceitar (Enter), descartar (D), pular (→), desenhar caixa (arrastar), classe (1–8), apagar (Del) |
| `vision/zona.py` | `ponto_no_poligono`, `centro`, `distancia` (em larguras: `proporcao` = altura/largura), `indice_insercao`, `poligono_simples`, `validar_zona` |
| `vision/dwell.py` | regra "parado na zona" (lógica pura): `AjustesParado.do_config(config, camera)`, `RegraParado` (`atualizar`, `expirar`, `encerrar`, `progresso`, `ativos`), `Aviso` (ALERTA/LIMPAR/PASSOU), `Progresso`, `SituacaoParado` |
| `eventos/alertas.py` | `ServicoAlertas` (thread "alertas"): resultados da IA → `RegraParado` por vista; `marcar_alerta` no evento; miniatura; `assinar(f(aviso, camera))`, `situacao(chave)`, `ativos()` (para a Fase 6) |
| `eventos/gravador.py` | `ServicoEventos` (thread "eventos"): atividade → evento, clipe CFR com atraso de `fim_evento_s`, foto/foto_limpa/miniatura/`deteccoes.json`, banco, `marcar_alerta(chave)` (Fase 5), `assinar_gravacao`/`assinar_remocao`, retenção periódica |
| `eventos/buffer.py`, `eventos/clipe.py`, `eventos/desenho.py` | `BufferQuadros` (pré-evento, só referências); `EscritorClipe` (PyAV H.264, reserva mp4v); `desenhar_caixas` |
| `eventos/retencao.py` | `pasta_do_evento`, `remover_evento`, `aplicar_retencao` (idade + limite de disco) |
| `eventos/coletor.py` | `ColetorTreino`: imagens "difíceis" + pré-rótulos YOLO em `dados/treino/pendentes/` (Fase 9) |
| `link/protocolo.py` | protocolo da placa só como TEXTO: `alerta`/`limpar`/`passou`/`camera`/`ping`/`hora` montam (≤ 64 bytes); `interpretar(bytes)` → `Joy`/`Botao`/`Modo`/`Pong`/`Ola` ou `ErroProtocolo` (E11) |
| `link/bitdog.py` | `LinkBitDog(config, endereco="")` (thread "placa", 1 socket UDP): PING/HORA, `reconfigurar(ip, porta_placa, porta_pc)`, status (`estado()` → `EstadoPlaca`), `ip_desta_maquina()`, alertas (`receber_aviso` = assinante do ServicoAlertas), botões A/J, câmera do joystick (`definir_cameras`, `definir_camera_joystick`), `assinar_joystick`/`assinar_botao`/`assinar_camera` |
| `tools/simulador_bitdoglab.py` | simulador AUTOSSUFICIENTE (só stdlib, sem importar o projeto): `PlacaSimulada` (lógica pura = referência do firmware; OLED "AGUARDANDO PC" com IP), `ConexaoPlaca(ip_pc=None)` (socket; aprende o PC pela origem do PING), `SimuladorApp` (Tkinter: OLED, matriz, buzzer, joystick, botões, log) |

## Estrutura (`bitDogLab/` — firmware, Fase 10; detalhes em `bitDogLab/README.md`)

| Caminho | Papel |
|---|---|
| `main.c` | laço principal: rede → pacotes (E7: responde à origem; PC novo → `MODO` antes do `PONG`) → entradas (20 ms) → `placa_tique` → buzzer/matriz/OLED (100 ms) → cão de guarda (8 s) |
| `placa.c/.h` | LÓGICA PURA (protocolo + estado), tradução 1:1 da `PlacaSimulada`; tempo em ms, joystick em centésimos; compila no PC |
| `rede.c/.h` | Wi-Fi assíncrono com reconexão (`cyw43_arch_wifi_connect_async` + `cyw43_tcpip_link_status`), país BR, sem economia de energia; UDP 5006, fila de 16 pacotes preenchida no callback (só copia), tirada com a trava do lwIP |
| `oled.c/.h`, `fonte5x7.h`, `telas.c/.h` | SSD1306 128x64 (I2C1 14/15, 0x3C, envia só se mudou, timeouts no I2C); fonte ASCII 0x20–0x7E; telas Wi-Fi / AGUARDANDO PC / MONITORANDO / ALERTA |
| `matriz.c/.h`, `ws2812.pio` | WS2812 (GPIO 7) pelo PIO (`pio_claim_free_sm_and_add_program_for_gpio_range`: o CYW43 também usa PIO); zigue-zague da BitDogLab; brilho 24/255 |
| `buzzer.c/.h` | PWM GPIO 10, padrões por tabela (ALTO 3x100 ms, MEDIO 800 ms), cala na hora com A ou NOITE |
| `entradas.c/.h` | botões A/B/J (5/6/22, repique de 40 ms); joystick X=ADC1 (GPIO 27), Y=ADC0 (GPIO 26), centro medido ao ligar, filtro, passos de 0,05 com folga |
| `config.h` | pinos, porta, tempos (iguais aos do simulador) |
| `wifi_config.h` | SSID/senha — FORA do Git; o CMake cria a partir de `wifi_config.exemplo.h` na 1ª vez; com o exemplo a placa mostra "FALTA O WI-FI" |
| `testes/rodar_testes.py` | compila com o MSVC (Build Tools 18, `vswhere`) e roda `teste_placa.c` (unidade) + `placa_cli.c` x simulador (60 mil eventos, respostas idênticas; o app aceita tudo o que o firmware manda) |
| `testes/ler_serial.py` | lê o diário da placa pela USB (stdlib; acha a COM USBSER; liga o DTR) |

## Decisões tomadas (não refazer sem motivo)

- Camera.`nome` é único (COLLATE NOCASE) e vira nome de pasta → não aceita `<>:"/\|?*`
  nem nomes reservados do Windows. `codigo`: `[A-Z0-9_-]{1,10}`; `-` sozinho = "nenhuma câmera".
- Canais numerados a partir de **1** (parâmetro `channel` das iCSee). `{stream}`: 0 principal, 1 sub.
- Coordenadas de caixas e zonas **normalizadas 0–1** (substream → principal é só multiplicar).
- Datas: ISO 8601 em horário local, com milissegundos.
- `Evento.pasta` é **relativa** a `dados/eventos/<nome da câmera>/` (ex.: `2026-10-11/213512`);
  renomear câmera = renomear uma pasta + UPDATE da câmera. Arquivos são relativos a `pasta`.
- Remover câmera apaga zonas/eventos do banco (CASCADE); apagar os arquivos é decisão da UI.
- Usuário/senha na URL: só `% & @ : / ? # espaço ; "` são codificados como `%XX` (firmwares
  Xiongmai não decodificam `%XX` em geral).
- `cv2.imwrite` falha no Windows com acentos no caminho → usar `salvar_jpg()` (imencode + tofile).
- RTSP via TCP por padrão (`captura.transporte_rtsp`); avisos do OpenCV/FFmpeg silenciados.
- **Windows: `os.environ` NÃO chega ao plugin FFmpeg do OpenCV** (`opencv_videoio_ffmpeg*.dll`
  usa `msvcrt.dll`, o Python usa UCRT — comprovado com servidor RTSP de teste). Toda variável
  `OPENCV_*` deve ser definida com `definir_variavel_ffmpeg()` (grava também via
  `ctypes.cdll.msvcrt._putenv`). Testes de regressão em `tests/test_captura.py`.
- Limites de vídeo separados dos de rede: `captura.timeout_abertura_s` (15) e
  `captura.timeout_leitura_s` (10). Estourar o tempo no meio de um pacote dessincroniza o
  RTSP/TCP e só chega "lixo" depois (`error while decoding MB`) — não apertar demais.
- pytest: `python_functions = ["test_*"]` (senão `testar_*` vira teste).
- Testes nunca usam o cofre real: fixture `cofre` (em memória) em `tests/conftest.py`. Testes de UI:
  `QT_QPA_PLATFORM=offscreen` (conftest) e SEMPRE uma CÓPIA do config.yaml (o app grava nele).
- **Vista = canal x lente** (decisão de 11/10/2026): câmeras que mandam 2 lentes numa imagem
  (`Camera.lentes` = vertical/horizontal) têm UMA conexão; o quadro é recortado por `Vista.recortar`.
  Zonas e eventos são por vista (colunas `vista` no banco, migração v2). `Camera.nomes_vistas`
  dá nomes ("Rua", "Calçada"); `Camera.lente_ptz` diz qual lente o motor move.
- **Threads e sinais Qt:** sinal emitido em OUTRA thread (ex.: `DistribuidorQuadros`) só pode ir
  para MÉTODO de um QObject da interface — uma `lambda` rodaria na thread de quem emitiu e mexeria
  em widgets fora da thread da UI (bug real encontrado nos testes). `ui/tarefas.executar` é seguro
  (o objeto de sinais vive na thread da UI; verificado).
- Sinal global `tema.sinais.tema_mudou`: conectar só MÉTODOS (o Qt desconecta ao destruir o
  widget); `lambda` ficaria pendurada e quebraria ao trocar o tema depois de remover um bloco.
- `QSpinBox` + QSS + `NoButtons` = texto some (bug do Qt). Usar `CampoNumero` (QLineEdit) ou combo.
- Ver a interface sem abrir janela: `widget.setAttribute(Qt.WA_DontShowOnScreen)`, `show()`,
  `grab().save(...)`. ATENÇÃO: importar `tests.conftest` força `QT_QPA_PLATFORM=offscreen` (sem as
  fontes do Windows → texto em fonte estranha); scripts de captura devem definir
  `QT_QPA_PLATFORM=windows` ANTES desse import.
- Navegação (Fase 2): clique (no "aperto") num bloco abre a Tela da câmera na vista clicada; num
  duplo clique o 2º clique cai no vídeo da Tela, que entra em tela cheia. Esc: sai da tela cheia,
  depois volta ao mosaico. Sair da Tela (Voltar/Esc/outra página/fechar app) ENCERRA o principal.
- Selo "HD · LxA · FPS" fica no RODAPÉ do bloco (no topo cobriria a data/hora da câmera).
- **IA (Fase 3):** roda no SUBSTREAM, por vista (lente recortada), `ia.ia_fps` (6) por vista, em lote
  na GPU. Resultados (`ResultadoVista`) chegam à UI pelo distribuidor (`deteccoes_prontas`); caixas
  normalizadas são desenhadas no `paintEvent` (vale para o principal sem conversão). Pesos em
  `pc/modelos/` (baixados na 1ª vez). `SETTINGS["sync"]` do Ultralytics é DESLIGADO (vem ligado).
  `camera.ajustes["ia.ia_fps"]` etc. sobrescrevem o config (`valor_da_camera`).
- **ByteTrack do supervision (pegadinhas comprovadas):** `lost_track_buffer` é em quadros A 30 FPS
  (usar `perdido_s * 30`; com `perdido_s * ia_fps` o ID morria em 0,5 s); objeto novo só ganha ID
  com conf >= limiar + 0,1 (usamos limiar = conf_deteccao - 0,1). `sv.ByteTrack` está obsoleto e
  SAI na 0.31 → `supervision<0.31` fixado no requirements; aviso silenciado.
- **Estabilidade do rastreio** (sintomas relatados pelo Diego na câmera real, com testes):
  objeto que o YOLO perde por instantes continua na tela por `ia.manter_perdido_s` (1 s) com
  `Deteccao.ausente_s > 0` (desenhado mais apagado) — a Fase 5 deve tratar como presente;
  mancha do MOG2 sobre objeto visto há pouco (ou que contém o objeto) não vira DESCONHECIDO;
  ID novo do ByteTrack em cima de objeto recém-perdido herda nosso ID, classe e "desde"
  (`ia.reassociar_sobreposicao`). IDs exibidos são nossos (1, 2, 3... por vista).
  ByteTrack só confirma objeto novo no 2º quadro seguido (exceto no 1º quadro do rastreador).
- DESCONHECIDO: YOLO com conf < `conf_minima` OU mancha do MOG2 sem objeto do YOLO em cima
  (`Deteccao.origem = "movimento"`, desenhada tracejada, sem %). ID que já foi reconhecido mantém
  a classe. Na câmera real (tarde de sol) aparecem DESCONHECIDOs de folhas/sombras: relevante para
  a Fase 5 (zona e ajustes `movimento_*`/`conf_deteccao` controlam o ruído).
- `ia_so_com_movimento`: sem movimento, o resultado anterior é repetido (`inferiu=False`) sem rodar
  o YOLO — objetos parados continuam detectados.
- Câmera de ARQUIVO/WEBCAM não abre "principal": a Tela da câmera usa o substream (abrir o arquivo
  2x tocava em outro ponto do vídeo e as caixas ficavam fora do lugar). Testes do caminho HD usam
  `GerenciadorDeTeste` (conftest): câmera RTSP cujo "endereço" é um vídeo (`camera.caminho`).
  Fixtures de teste usam `ia.fonte: substream` (o config real usa `principal`).
- FPS do leitor = quadros nos últimos 2 s (`JANELA_FPS_S`). Média de "1/intervalo" dava 32 FPS num
  stream de 12 (rajadas do Wi-Fi + buffer do FFmpeg ao conectar) — teste de regressão existe.
- **Eventos (Fase 4):**
  - Atividade = regiões de movimento OU objeto reconhecido que APARECEU há menos de
    `objeto_novo_s` (carro estacionado não prende o evento; objetos presentes nos primeiros
    `aquecimento_s` não abrem evento), em `confirmar_resultados` análises seguidas.
    `eventos.area: zona` usa a zona (Fase 5) para filtrar.
  - O clipe é SEMPRE do substream, a `fps_clipe` (taxa constante), escrito com atraso de
    `fim_evento_s` a partir de um `BufferQuadros` (janela max(pre, fim) + 2 s; guarda só
    referências, porque `cap.read()` devolve arrays novos). Gravar o HD exigiria GBs de RAM →
    ideia futura: remux dos pacotes H.264 (sem decodificar). A FOTO sai da imagem que a IA
    analisou (HD com `ia.fonte: principal`); o histórico do evento guarda as detecções SEM imagens
    (só a melhor candidata a foto tem imagem).
  - Pastas: `YYYY-MM-DD/HHMMSS[_vN][-n]`; arquivos `clipe.mp4`, `foto.jpg` (com caixas),
    `foto_limpa.jpg`, `miniatura.jpg` (320 px), `deteccoes.json` (detecções por instante do clipe;
    o player desenha as do instante mais próximo, até 0,5 s).
  - PyAV: `movflags=+faststart` vai no `av.open(..., options=...)` (`container.options` não aceita).
  - Windows não apaga arquivo aberto: `Reprodutor.fechar_e_esperar()` (BlockingQueued) ANTES de excluir.
  - `cancelar_assinatura` compara com `!=` (método ligado é recriado a cada acesso; `is` falhava).
  - Câmera com `gravar` e sem `deteccao` é analisada só por movimento (sem YOLO): eventos sem caixas.
  - Memória na câmera real: IA ~2,2 GB (PyTorch/CUDA); eventos +~200 MB.
- **Alertas (Fase 5), regra em `vision/dwell.py`:**
  - Relógio por "âncora": parado enquanto o centro fica a menos de `limite_movimento` (em LARGURAS
    da imagem: y é multiplicado por altura/largura) do ponto onde parou; afastou → âncora nova.
  - Aquecimento = "ignorado ATÉ SE MOVER" (mais que o limite a partir de onde foi visto pela 1ª vez).
    Câmera sem resultado por mais de `limpar_apos_sumico_s` → aquecimento recomeça (reconectou).
  - Troca de ID: ID novo perto (≤ limite, mesma classe ou DESCONHECIDO) de um objeto PARADO que sumiu
    (alertado, em cooldown ou ignorado — guardado por `cooldown_s`) herda alerta/cooldown/ignorado.
    O LIMPAR usa o id do ALERTA original (a placa só conhece esse).
  - Sumiço dentro da zona: o relógio zera depois de `tolerancia_sumico_s`, mas o PASSOU só sai quando
    o objeto é esquecido (`limpar_apos_sumico_s`) — antes, uma oclusão de 3 s gerava PASSOU falso.
    PASSOU exige `passou_min_s` na zona e não sai para DESCONHECIDO (`passou_desconhecido`).
  - `ServicoAlertas`: `expirar` a cada 0,2 s (LIMPAR mesmo com a câmera sem imagem); `numero` do
    resultado voltando = IA recriou a vista (IDs recomeçam) → `encerrar` (LIMPAR "camera").
  - `marcar_alerta(chave, desde)`: evento aberto pelo alerta começa quando o objeto PAROU (não antes
    do fim do evento anterior) — senão a duração era "0 s".
  - Zona salva arredondada a 5 casas (igual ao banco); `TelaCamera.zona_salva` → janela chama
    `definir_cameras` de eventos e alertas (eles releem a zona do banco).
  - Toast com texto quebrado ficava cortado: altura = `layout.totalHeightForWidth(LARGURA)`.
  - Bug do Diego (12/10): na aba "Lente 2", o botão Zona editava a Lente 1 (`editar_zona` sem número
    pegava a 1ª vista da CÂMERA, não a visível). Agora usa `vistas_visiveis()[0]`; teste com câmera de 2
    lentes (`test_zona_da_lente_2_fica_na_lente_2`) — os testes antigos só tinham câmeras de 1 lente.
- **Placa (Fase 6):**
  - Simulador não importa nada do projeto (pode ser copiado para outro PC e testado pelo Wi-Fi) e
    tem o próprio leitor de mensagens, como o firmware: um erro no lado do PC não se esconde atrás
    do mesmo código. `test_simulador_aceita_tudo_o_que_o_pc_monta` confere que os dois concordam.
  - Teclas do simulador: setas/WASD = joystick; botão A = Espaço ou 1 (a tecla A fica com o
    joystick), B = B ou 2, J = J ou 3.
  - `placa.ip` preenchido (Ajustes → Placa) → o PC manda PING para lá e só aceita pacotes desse IP;
    vazio → espera a placa se apresentar (OLA) e aprende o IP dela.
  - "Ao conectar" (1º PONG depois de desconectada) o PC manda HORA, CAM e o alerta ativo — o OLA
    não é necessário. A placa, ao conhecer um PC novo, manda `MODO;<atual>`.
  - Porta do PC ocupada não derruba a thread: o erro aparece em Ajustes e `reconfigurar` reabre.
  - O app escuta em todas as interfaces (a placa vem pelo Wi-Fi: o Windows pode perguntar sobre o
    firewall — permitir em "Redes privadas"); testes usam `endereco="127.0.0.1"` (sem pergunta).
  - Windows: `SIO_UDP_CONNRESET` desligado nos dois lados (senão, depois de enviar para uma porta
    fechada, o próximo `recvfrom` falha com ConnectionResetError).
  - Alertas na placa: chave (código, id); escolhido = maior nível, empate o mais recente; reenvio a
    cada `reenvio_alerta_s` com segundos = `aviso.segundos` + tempo desde o alerta; LIMPAR em dobro
    só se aquele alerta chegou a ser enviado; BTN;A silencia o exibido e o próximo ativo assume.
    OLA → HORA, CAM, alerta ativo e um PING (conecta na hora).
  - Câmera do joystick: só câmeras com PTZ. A janela chama `definir_camera_joystick` ao abrir a câmera
    e no menu do bloco; BTN;J cicla; nenhuma com PTZ → `CAM;-`. A Fase 7 usa `assinar_joystick` +
    `camera_joystick`.
  - Mensagens inválidas: no máximo 1 aviso no log a cada 5 s (as demais são contadas).
  - Correção no docs/protocolo.md (seção 8, nota para o firmware): a maior mensagem tem 55 bytes, não
    56 (conta errada; o formato não mudou).
- **PTZ (Fase 7):**
  - `ServicoPTZ` numa thread (ONVIF leva centenas de ms): no máximo `ptz.max_comandos_s`; câmera
    andando sem comando por `ptz.timeout_joystick_s` → para (segurança); enquanto alguém segura, o
    movimento é reenviado a cada `ptz.reenviar_movimento_s` (algumas câmeras param sozinhas).
    Quem segura (painel, teclado, joystick E5) repete o comando. Fechar o app para as câmeras.
  - Câmera de arquivo/webcam = `PTZFalso`; ONVIF falhou → `PTZFalso` + aviso no painel.
  - `ptz.inverter_x/y` invertem SÓ o comando do motor (o indicador mostra o que foi pedido).
  - A lente girou: depois de parar (+`estabilizar_s`, o vídeo chega atrasado) ou de ir a uma posição
    (+`tempo_preset_s`), `assinar_vista_movida` avisa a vista da `lente_ptz`: a IA reinicia o MOG2
    (`ServicoDeteccao.reiniciar_fundo` marca; a thread da IA reinicia) e a regra de parado recomeça o
    aquecimento (`RegraParado.reiniciar`, mantém alertas ativos) — senão a cena inteira vira
    movimento e o carro estacionado (agora em outro lugar da imagem) vira alerta. Cancelado se voltar a andar.
  - Presets: `ir_preset` procura pelo nome "Sentinela N" (ou token N); `salvar_preset` tenta com o
    número e, se a câmera recusar, salva sem número (ela escolhe o token).
  - Setas do teclado: filtro de eventos no QApplication enquanto o painel PTZ está visível (ignora
    auto-repetição e campos de texto). Painel ancorado DENTRO do vídeo da lente com motor.
  - Joystick da placa → `ptz.mover(link.camera_joystick, x, y)` (main.py); botão J → `parar_outras`.
- **Ajustes (Fase 8):**
  - Tudo vale SEM reiniciar (exceto `geral.pasta_dados`): `JanelaPrincipal._ajustes_salvos` aplica
    cada chave — tema, layout, `distribuidor.definir_fps` (o timer vive na thread do distribuidor:
    pedido por sinal), `core.logs.definir_nivel`, `deteccao.recarregar_modelo()` (troca na thread da
    IA; se o novo falhar, fica o antigo), `eventos.recarregar_ajustes()` (Parametros + tamanho do buffer
    + coletor), `ptz.recarregar_ajustes()`; `ia.*`/`parado.*`/câmeras → `recarregar_cameras()` (IA
    recria a vista se a assinatura mudar — inclui `movimento_area_minima`; alertas releem `AjustesParado`).
  - Por câmera (`camera.ajustes`, banco): `eventos.area`, `ia.ia_fps`, `ia.conf_minima`,
    `ia.ia_so_com_movimento`, `ia.movimento_area_minima`, `parado.cooldown_s`, `parado.aquecimento_s`,
    `parado.tempo_parado`/`parado.nivel` (dicts só com as classes mudadas), `ptz.inverter_x/y`. Chaves
    desconhecidas são PRESERVADAS. `ServicoCameras.salvar_ajustes` não mexe na senha (o `editar` grava a senha).
  - Sair dos Ajustes com alterações pergunta (Salvar/Descartar/Cancelar); trocar de câmera também.
  - Sem `QDoubleValidator`: ele segue o idioma do Windows e recusaria "0.5"; `EditorAjuste.erro()`
    valida e aceita vírgula ou ponto. Inteiros saem como `8` (não `8.0`) no YAML.
  - Widget trocado: `hide()` ANTES de `deleteLater()` — o deleteLater só age no laço de eventos e o
    formulário antigo aparecia por cima do novo (visto nas capturas).
  - `ServicoEventos.aplicar_retencao` tem trava (limpeza automática x botão "Limpar agora").
  - Capturas do README em `docs/capturas/*.jpg`, geradas SÓ com dados de teste (vídeos gerados e fotos
    de exemplo do Ultralytics) — nunca imagens da câmera do Diego.
- **Aprendizado contínuo (Fase 9):**
  - O treino roda num PROCESSO à parte (`python -m treino.ciclo`): CPU/GPU pesadas por minutos sem
    travar a janela, prioridade baixa (Windows: `BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW`) e, se
    cair, o app não cai. Canal de avisos = uma linha JSON por aviso no stdout (o `sys.stdout` das
    bibliotecas é desviado para o stderr); log próprio em `dados/logs/treino.log`.
  - Windows: os ajudantes do DataLoader REIMPORTAM o módulo principal — script sem
    `if __name__ == "__main__"` quebra ("DataLoader worker exited unexpectedly"). Validação usa
    `workers=0`; treino usa `treino.ajudantes` (0 = padrão; cai para 0 se falhar).
  - **Memória (erro de 12/10/2026, PC de 8 GB de RAM):** o que acaba no Windows é a memória
    RESERVÁVEL (commit = RAM + arquivo de paginação), não a RAM. Medido: cada GB que o PyTorch reserva
    na PLACA conta 1 GB de commit; o treino (YOLO11s, 1280) custa ~4,5 GB + a placa (lote 1: 0,9;
    2: 1,7; 4: 3,1; 8: 5,1 GB → ~10 GB no total com lote 8); cada ajudante do DataLoader reimporta o
    torch (~1,5 GB). Com o app (~4 GB) + 2 ajudantes, o commit acabou: o OpenCV do app falhou ("Insufficient
    memory"), o `_tique` do distribuidor quebrou (agora pula o quadro, `_avisar_falha`), um ajudante caiu
    com a janela "Erro de Aplicativo". Defesas: `treino.ajudantes: 0`; portão `memoria_minima_gb` (o
    serviço ADIA a rodada automática 30 min sem gastar o dia; "Treinar agora" explica; o ciclo confere de
    novo e não grava no histórico); `limitar_placa()` antes de cada etapa pesada
    (`torch.cuda.set_per_process_memory_fraction` com `orcamento_placa`; acima dele o PyTorch dá
    `OutOfMemoryError` e o lote cai pela metade — Ultralytics na 1ª época, nosso laço depois);
    vigia por imagem/lote (`criar_vigia`: abaixo de `memoria_critica_gb` → `MemoriaInsuficiente`, rodada
    registrada como "parou para não faltar memória"); `SetErrorMode` no processo de treino (sem a janela
    de erro, que segurava a memória do processo quebrado). `MemoriaInsuficiente` NÃO é `RuntimeError`
    (o laço do Ultralytics trata alguns `RuntimeError`). Teste real (cópia dos dados): com 4 GB de
    "lastro" simulando o app, o treino parou sozinho com 2,6 GB livres; sem lastro, treinou as 12 épocas
    com no mínimo 2,2 GB livres. Na máquina do Diego (VS Code, Steam etc. abertos) sobram ~6,3 GB com o
    app aberto — menos que o portão (7 GB): recomendado aumentar o arquivo de paginação (README).
  - **Canal de avisos em UTF-8** (`canal_de_avisos`, + `PYTHONIOENCODING=utf-8` no `Popen`): no Windows a
    saída num "pipe" é cp1252, sem "→" — o aviso final "melhorou: nota 0.852 → 0.868" derrubava o processo,
    o app registrava "cancelado" e NUNCA trocava o modelo aprovado (aconteceu em 12/10, 08:57).
  - **Viés de seleção na prova (achado em 12/10, medido numa cópia):** o Ultralytics avalia a cada época
    nas imagens de VALIDAÇÃO e guarda a melhor (`best.pt`, e a `paciencia` também olha para elas); depois a
    promoção usa as MESMAS imagens. O `sentinela_20261012_0857` (aprovado: 0,852 → 0,868 nas 24 imagens
    da rodada dele) deu, nas 5 imagens de validação que chegaram DEPOIS, 0,635 contra 0,822 do yolo11s
    (PESSOA 0,52 x 0,78); nas 29 de hoje, 0,761 x 0,830. Ele nunca entrou em uso (bug do "→") — não usar.
    **Corrigido (aprovado pelo Diego em 12/10):** o treino roda com `val=False` (sem "melhor época" e sem
    parada antecipada — `treino.paciencia` saiu do config), `treino.epocas` fixas, e usa o `last.pt`; a
    validação é vista UMA vez, na decisão (teste `test_treino_nao_olha_a_validacao`). Com `val=False` o
    Ultralytics ainda valida na última época e no `final_eval` (só relatório; nada é escolhido).
  - Mantemos a "cabeça" COCO de 80 classes (rótulos com ids COCO; `names` do modelo no dados.yaml):
    o modelo treinado é carregado pelo `ModeloYolo` sem mudança nenhuma.
  - Receita com poucas imagens: partir do modelo atual, TODAS as rotuladas, `freeze=treino.congelar`
    (10). Experimento real (12/10, 64 treino/24 validação, 20 épocas): congelando 0,675 → 0,688
    (aprovado); sem congelar 0,675 → 0,684 (não passa da margem). Treino na GPU varia um pouco a cada
    rodada (outra rodada deu 0,683).
  - O Ultralytics chama `on_fit_epoch_end` uma vez a mais no fim (época 4 de 3): limitado.
  - Rodadas que não treinam (poucas imagens novas) NÃO entram no histórico (só o resultado na hora).
  - "Imagens novas" = rotuladas DEPOIS do FIM da última rodada que treinou (12/10: comparar com o início
    contava de novo as rotuladas durante ela; achado por um teste instável; `test_rotuladas_durante_a_rodada...`).
  - Quem troca o modelo em uso é a janela (`usar_modelo`: `ia.modelo` + `deteccao.recarregar_modelo`);
    com `treino.trocar_automatico` ela troca sozinha quando a rodada é aprovada.
  - O coletor grava o `.jpg` POR ÚLTIMO (o processo de treino só "enxerga" a amostra pelo .jpg).
- **Firmware (Fase 10):**
  - A lógica da placa é UMA só, em C puro (`placa.c`), igual à `PlacaSimulada`; o simulador continua sendo
    a referência. Mudou um? Mude o outro e rode `bitDogLab/testes/rodar_testes.py` (compara os dois).
  - Compilar no PC: Build Tools do Visual Studio 18 (`cl /std:c11 /W4 /WX`) via `vcvars64.bat` num `.bat`
    temporário (o `cmd` não entende as aspas escapadas do `subprocess` numa linha só).
  - Compilar o firmware pela linha de comando: `PICO_SDK_PATH=~/.pico-sdk/sdk/2.3.1`, PATH com
    `toolchain/15_2_Rel1/bin`, `cmake/v4.3.4/bin`, `ninja/v1.13.2`; `cmake -S . -B build -G Ninja` + `ninja -C build`.
    `-Wall -Wextra` só nos nossos .c (`set_source_files_properties`); zero avisos.
  - O joystick anda em passos de 0,05 na camada de hardware (`entradas.c`), não em `placa.c` (que precisa
    ficar igual ao simulador); a zona morta (0,12) continua na lógica.
  - A placa NÃO guarda o endereço do PC na flash: o PC manda PING a cada 2 s, então ela o reencontra
    sozinha; ao voltar o Wi-Fi, se já conhecia o PC, manda OLA (`placa_wifi_voltou`).
  - Nunca copiar SSID/senha de Wi-Fi de outros projetos ou exemplos para este (o `wifi_config.h` fica fora do Git).
- **Rodada de 12/10 (pedido longo do Diego), achados medidos:**
  - "Objetos que não existem / que já sumiram": com `ia_so_com_movimento`, sem movimento o último resultado
    era REPETIDO inteiro — manchas de movimento (DESCONHECIDO) e fantasmas (`ausente_s > 0`) ficavam
    congelados, pareciam PARADOS e viravam ALERTA na placa. Agora `repetir_sem_movimento` só repete o que
    o YOLO viu, e, se há objetos, o YOLO CONFERE a cada `ia.verificar_parado_s` (2 s). Teste
    `test_cena_parada_confere_se_o_objeto_ainda_esta_la` (falha sem a correção).
  - Zona: sempre foi por lente; o "vazamento" era a lente SEM zona = imagem inteira (`ponto_no_poligono`
    com zona None devolve True). Agora `RegraParado.vigiando` + `parado.sem_zona` (padrão "ignorar"); os
    eventos com `eventos.area: zona` seguem o mesmo ajuste (`Parametros.sem_zona_vigia_tudo`). A mensagem
    ao apagar a zona acompanha o ajuste. `ponto_no_poligono` NÃO mudou (outros usos).
  - `ia.identificar` (classes) filtra DEPOIS do rastreador (tela, eventos, alertas, coletor); está na
    assinatura da vista (mudou → a IA recria). DESCONHECIDO desligado → mancha de movimento sozinha não
    abre evento (`Parametros.movimento_abre`; câmera sem IA continua só com movimento).
    `parado.alertar` (falta = sim) na tabela de classes (coluna ALERTA; por câmera "Padrão/Sim/Não").
  - Engasgos do vídeo: medido na câmera real (substream 12 FPS): 25% dos quadros chegam "grudados",
    buracos de até 377 ms. O stream informa a hora da câmera (`CAP_PROP_POS_MSEC`, 83 ms certinhos).
    `RelogioExibicao` (pts + menor atraso dos últimos 5 s + `suavizar_s`, nunca volta no tempo) +
    `LeitorVideo.quadro_para_exibir` (só a TELA; IA/gravação usam `ultimo_quadro`) + distribuidor com
    tique de 15 ms e limite por bloco = `fps`. Real (30 s): sem suavizar 10,3 img/s, desvio 39 ms, máx 311,
    5 travadas > 200 ms; com 250 ms: 12,1 img/s, desvio 14 ms, máx 125, 0 travadas. Ao conectar, a
    1ª rajada sai junta (sem histórico ainda) — aceitável.
  - `picotool load -f -x` deixou a placa em BOOTSEL (RPI-RP2); `picotool reboot` resolveu. A placa vê o
    PC como 192.168.0.102 (WAN do roteador 2): a conversa atravessa o NAT como previsto (decisão 4).
  - Testes que dependem do config do Diego agora fixam: `ia_so_com_movimento` (test_visao), `sem_zona`
    e cooldown etc. da especificação (test_dwell), `eventos.area` (test_eventos), `ptz.inverter_x/y`
    (fixture `contexto` e test_ptz) — ele ligou `ptz.inverter_x: true`.
- Testes: o fixture `contexto` força `placa.ip = ""` (o config.yaml real tem o IP do notebook do Diego).
- **GitHub (12/10, pedido do Diego):** repositório PÚBLICO `git@github.com:DiegoSousaOliveira/manitoramento_cmr_BitDogLab.git`,
  branch `main`, licença MIT (o README avisa que o Ultralytics é AGPL-3.0).
  - O `pc/config.yaml` do Git tem os valores PADRÃO (yolo11n, `ia_fps` 6, `dispositivo: auto`, `conf_minima`
    0.40, `eventos.area: quadro`, cooldown 120, aquecimento 20, treino às 03:00, `placa.ip` vazio,
    `inverter_x: false`). A cópia do Diego, com os ajustes dele, está com
    `git update-index --skip-worktree pc/config.yaml` (o Git não enxerga as mudanças locais). Para mudar
    o padrão publicado: `--no-skip-worktree`, commitar SÓ a linha nova (não os ajustes dele), voltar o skip.
  - Nunca publicar: `pc/dados/` (banco, eventos e imagens da câmera real), modelos `.pt`, vídeos,
    `bitDogLab/wifi_config.h`, `bitDogLab/build/`. Conferir `git status` antes de cada commit.
  - A chave SSH do Diego tem senha (passphrase): o `git push` é ele quem roda.
- Ultralytics 8.4.175 trocou `half=True` por `quantize=16` e avisava a CADA inferência:
  `ModeloYolo` usa o nome que a versão instalada conhece (`DEFAULT_CFG_DICT`).

## Câmera real do Diego (diagnóstico de 11/10/2026, confirmado com fotos)

- Xiongmai **XM232_X6S-WEQ-H_8M** (duas lentes, PTZ), firmware V5.04, servidor RTSP "H264DVR 1.0".
- **As duas lentes vêm numa ÚNICA imagem, empilhadas na vertical**: em cima a rua (ampla, com a
  data/hora), embaixo a calçada (com "CAM01"). Emenda exatamente na metade (medido).
  - substream 640x720 = 2 x 640x360; principal 2304x2592 = 2 x 2304x1296 (cada lente 16:9).
  - `channel=1` e `channel=2` entregam a MESMA imagem (a câmera ignora o canal).
- Principal abre em 2,7–9,4 s e roda a ~12 FPS (por isso o limite de abertura de 15 s). No app,
  a alta resolução chega em ~8 s (o substream aparece antes). Verificado em 11/10/2026.
- ONVIF só expõe o canal 1 (resoluções informadas pelo ONVIF não batem com as reais);
  PTZ com movimento contínuo. **O motor move a lente de BAIXO (calçada) → `lente_ptz = 2`.**
- Cadastro recomendado: RTSP, canal 1, lentes "empilhadas", PTZ ligado, lente com motor = 2.
  Cadastrada pelo Diego como "Câmera01" (id 1) — estava com lente_ptz = 1 (avisado para trocar).
- Primeira resposta RTSP ~2 s; as seguintes ~100 ms (Wi-Fi em economia de energia).
- IA na câmera real (11/10/2026): pronta em 4,5 s, 11,6 imagens/s (2 lentes), lote ~23 ms na
  RTX 5060, ~1% de CPU do processo (substream). Com IA em HD: 10,8 imagens/s, lote ~32 ms, ~6% de CPU.
  O "fundo" da rua ocupa ~80x40 px no substream (moto distante ≈ 10x8 px: invisível para a IA).
- Eventos na câmera real (11/10/2026, pasta temporária): 3 eventos em 50 s (PESSOA, MOTO), clipe
  640x360, foto 2304x1296; coletor guardou "duvida" e "amostra". IA 10 imagens/s, ~5% de CPU.
- Alertas na câmera real (11/10/2026, 21h30, noite/IR, sem zona, pasta temporária): 90 s sem alerta
  falso; 1 PASSOU (CARRO na rua). Teste com IA real (zidane.jpg parada): 1 único ALERTA em 8,0 s.
- PTZ real (12/10/2026, só CONSULTA, sem mover): ONVIF conecta em 2 s, perfil com PTZ `000`, motor
  `IDLE`, nenhum preset salvo; a posição informada é (-1, -1) (a câmera não informa a real).
  O cadastro ainda está com `lente_ptz = 1` — o motor é da lente 2 (Diego precisa corrigir).
- Treino com os dados reais (12/10/2026, CÓPIA temporária, sem mexer nos dados): 96 imagens coletadas
  (67 "dúvida", 18 "amostra", 11 "desconhecido"); o professor (YOLO11x) rotulou 88 com certeza e mandou
  8 para revisão em ~5 s; separação 64 treino / 24 validação; nota do yolo11n 0,675 (PESSOA 0,52,
  MOTO 0,83); 20 épocas em ~45 s na RTX 5060; rodada completa (processo à parte) em ~56 s.
- O Diego mudou pelo Ajustes (12/10): `ia.modelo = yolo11s.pt`, `ia.ia_fps = 1`, `treino.limite_gb = 10`;
  depois `ia.fonte = substream`, `ia.ia_so_com_movimento = true`, `treino.memoria_minima_gb = 7`,
  `treino.memoria_critica_gb = 1` (o `test_servico_detecta_em_lote_no_ritmo_da_ia` quebrou por isso: fixture corrigida).
  Testes que dependem desses valores definem os seus (o config real é do usuário).
- **Rede do Diego (verificado em 11/10/2026):** PC `192.168.1.101` por CABO no roteador 2
  (`192.168.1.1`), que está "atrás" do roteador principal (rede `192.168.0.x`, onde fica a câmera
  `192.168.0.100`; tracert PC → 192.168.1.1 → câmera). O roteador 2 faz NAT: aparelhos da `0.x` não
  conseguem INICIAR conversa com o PC — por isso o PC puxa a conversa com a placa (decisão 4).
- PyTorch: `torch 2.14.1+cu130` (RTX 5060 = sm_120 exige CUDA ≥ 12.8; driver CUDA 13.3).

## Protocolo BitDogLab (resumo — detalhes e esclarecimentos E1–E11 em docs/protocolo.md)

UDP; PC escuta **5005**, placa escuta **5006**. ASCII sem acento, campos `;`, termina em `\n`,
≤ 64 bytes, decimais com ponto. `<CAM>` = código do cadastro.

PC → placa: `ALERTA;<CAM>;<CLASSE>;<seg>;<conf>;<id>;<NIVEL>` · `LIMPAR;<CAM>;<id>` ·
`PASSOU;<CAM>;<CLASSE>` · `CAM;<CAM>` · `PING;<seq>` (2 s) · `HORA;<HH:MM>` (60 s e ao conectar).

Placa → PC: `JOY;<x>;<y>` (-1.00..1.00, só quando muda, ≤ 20/s; `0.00;0.00` = parar) ·
`BTN;<A|B|J>` · `MODO;<NORMAL|NOITE>` · `PONG;<seq>` · `OLA`.

Regras: 6 s sem PONG = desconectada; `OLA` → reenviar HORA, CAM e alerta ativo; `BTN;A` silencia
(não reenviar); `BTN;J` troca câmera e responde `CAM`; vários alertas → maior nível (empate: mais
recente); mensagens inválidas → ignorar + log.
