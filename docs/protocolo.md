# Protocolo PC ⇄ BitDogLab (UDP)

> **Status: FIXO.** O formato das mensagens abaixo não deve ser alterado — o firmware em C
> da placa (Raspberry Pi Pico W, pasta `bitDogLab/`) segue este documento.
> A seção 6 ("Esclarecimentos") detalha comportamentos que o texto original deixava em
> aberto, **sem mudar nenhum formato**. Única adição (12/10/2026, pedido do Diego): a mensagem
> `SOM` (E13), que placas antigas apenas ignoram (comando desconhecido); e o botão A ganhou
> 1 toque / 2 toques (E14).

## 1. Transporte

| Item | Valor |
|---|---|
| Protocolo | UDP sobre Wi-Fi (rede local) |
| Porta em que o **PC** escuta | **5005** |
| Porta em que a **placa** escuta | **5006** |
| Mensagens por datagrama | exatamente **uma** |
| Tamanho máximo | **64 bytes**, incluindo o `\n` final |
| Codificação | ASCII imprimível (0x20–0x7E), **sem acentos** |
| Separador de campos | `;` |
| Fim da mensagem | `\n` (0x0A) |
| Números decimais | sempre com **ponto** (`0.87`, nunca `0,87`) |
| Maiúsculas | comandos, classes, níveis e códigos são sempre MAIÚSCULOS |

Endereços — **como a placa e o PC se encontram** (decisão de 11/10/2026, pedida pelo Diego):
1. A placa entra no Wi-Fi e mostra no OLED **"AGUARDANDO PC"**, o **IP dela** e a **porta** (5006).
2. A pessoa digita esse IP no app (Ajustes → Placa BitDogLab; fica em `placa.ip` no `config.yaml`).
3. O PC manda `PING` para `<IP da placa>:5006` a cada 2 s — é o PC que puxa a conversa.
4. A placa responde (e manda tudo depois: `JOY`, `BTN`...) para o **endereço de origem** do último
   pacote válido do PC: o IP **e a porta** de onde ele veio (E7).

Assim a placa **não precisa ter o IP do PC no firmware**, e funciona até com um roteador no meio
(o roteador deixa passar a resposta a um pacote que o PC mandou). Modo alternativo: com `placa.ip`
vazio, o PC aprende o IP da placa pelo primeiro pacote dela (ex.: `OLA`, se o firmware souber o IP
do PC). Com `placa.ip` preenchido, o PC ignora pacotes de outros endereços.

## 2. Tipos de campo

| Campo | Formato | Exemplo |
|---|---|---|
| `<CAM>` | código da câmera do cadastro: 1–10 caracteres `A-Z 0-9 _ -`, com ao menos uma letra ou dígito. `-` sozinho = "nenhuma câmera" | `FRENTE` |
| `<CLASSE>` | `PESSOA`, `BICICLETA`, `CARRO`, `MOTO`, `ONIBUS`, `CAMINHAO`, `GATO`, `CACHORRO`, `DESCONHECIDO` | `PESSOA` |
| `<seg>` | inteiro de 0 a 99999: segundos parado (arredondado para baixo) | `12` |
| `<conf>` | decimal de `0.00` a `1.00`, **2 casas** | `0.87` |
| `<id>` | inteiro de 1 a 999999: ID de rastreamento do objeto (único por câmera) | `7` |
| `<NIVEL>` | `BAIXO`, `MEDIO`, `ALTO` | `ALTO` |
| `<x>`, `<y>` | decimal de `-1.00` a `1.00`, **2 casas** | `-0.80` |
| `<seq>` | inteiro de 0 a 65535 (volta a 0 depois de 65535) | `42` |
| `<HH:MM>` | hora local do PC, 24 h, sempre 2 dígitos | `09:05` |

## 3. Mensagens PC → Placa

| Mensagem | Exemplo | Significado |
|---|---|---|
| `ALERTA;<CAM>;<CLASSE>;<seg>;<conf>;<id>;<NIVEL>` | `ALERTA;FRENTE;PESSOA;12;0.87;7;ALTO` | objeto parado na zona |
| `LIMPAR;<CAM>;<id>` | `LIMPAR;FRENTE;7` | objeto alertado saiu |
| `PASSOU;<CAM>;<CLASSE>` | `PASSOU;FRENTE;CARRO` | passou sem parar (informativo) |
| `CAM;<CAM>` | `CAM;FRENTE` | câmera que o joystick controla agora |
| `PING;<seq>` | `PING;42` | a cada 2 s |
| `HORA;<HH:MM>` | `HORA;21:35` | a cada 60 s e ao conectar |
| `SOM;<UMA\|CONTINUO>` | `SOM;CONTINUO` | como o buzzer toca (E13): ao conectar e quando muda nos Ajustes |

## 4. Mensagens Placa → PC

| Mensagem | Exemplo | Significado |
|---|---|---|
| `JOY;<x>;<y>` | `JOY;-0.80;0.00` | joystick de -1.00 a 1.00 (só quando muda, até 20/s) |
| `JOY;0.00;0.00` | | joystick solto → parar câmera |
| `BTN;<A\|B\|J>` | `BTN;J` | A = alerta atual calado na placa (2 toques, E14), B = trocar modo, J = próxima câmera com PTZ |
| `MODO;<NORMAL\|NOITE>` | `MODO;NOITE` | modo atual da placa (informativo) |
| `PONG;<seq>` | `PONG;42` | resposta ao PING |
| `OLA` | `OLA` | placa ligou/conectou |

## 5. Regras (originais)

1. 6 s sem `PONG` → placa "desconectada" (o app mostra na barra lateral).
2. Ao receber `OLA`: o PC reenvia `HORA`, `CAM`, `SOM` e o alerta ativo, se houver.
3. `BTN;A` → o alerta atual foi calado NA PLACA; o PC **continua** a reenviá-lo (E14, mudou em
   12/10/2026 — antes o PC parava e o alerta sumia da placa em 6 s).
4. `BTN;J` → o PC troca a câmera controlada e responde `CAM`.
5. Vários alertas ao mesmo tempo: o PC envia o de **maior nível** (empate: o **mais recente**).
6. Mensagens inválidas são ignoradas e registradas no log; nunca derrubam o programa
   (vale para o PC e para a placa).

## 6. Esclarecimentos (comportamento, sem mudar formatos)

Estes pontos completam as regras acima para que PC, simulador e firmware se comportem igual.

**E1. Reenvio do alerta ativo.** Como UDP pode perder pacotes, enquanto houver um alerta
ativo **não silenciado**, o PC reenvia o mesmo `ALERTA` a cada 2 s (`placa.reenvio_alerta_s`),
com `<seg>` atualizado. A placa trata um `ALERTA` com o mesmo `<CAM>` e `<id>` do que já
está na tela como **atualização** (atualiza o OLED, **não** toca o buzzer de novo).

**E2. Expiração na placa.** Se a placa ficar 6 s sem receber o reenvio do alerta exibido,
ela volta ao repouso (protege contra um `LIMPAR` perdido). Por isso o alerta silenciado
(que o PC deixa de reenviar) some da tela da placa em até 6 s.

**E3. `LIMPAR` em dobro.** O PC envia `LIMPAR` duas vezes (na hora e 1 s depois). A placa
ignora `LIMPAR` de um `<CAM>`/`<id>` que não está exibindo.

**E4. Seleção do alerta.** Entre os alertas ativos, vale o de maior nível (empate: o que
começou mais recentemente). Quando a escolha muda (ex.: surgiu um `ALTO`), o PC envia o novo
`ALERTA` na hora; a placa sempre exibe o último `ALERTA` recebido. (`BTN;A` não muda a escolha:
quem cala é a placa — E14.)

**E5. Joystick mantido.** "Só quando muda" faria a câmera parar pela regra de segurança do
PC (3 s sem `JOY` com a câmera em movimento → parar). Então, **enquanto o joystick estiver
fora do centro**, a placa repete o último `JOY` a cada 1 s. Continua valendo o limite de 20/s.

**E6. Eixos e zona morta.** `x > 0` = direita, `y > 0` = para cima (a placa inverte o eixo do
ADC se precisar). A placa aplica a zona morta: dentro dela envia `0.00`. Ao voltar ao centro
envia `JOY;0.00;0.00` uma vez.

**E7. Destinos.** O PC envia para `<IP da placa cadastrado (ou aprendido)>:<porta da placa>`
(padrão 5006) — sempre essa porta, mesmo que o pacote da placa tenha vindo de outra porta de
origem. A placa envia para o **endereço de origem (IP e porta)** do último pacote válido recebido
do PC. Na mesma rede isso é `<IP do PC>:5005` (o PC envia a partir da porta 5005); com um roteador
no meio, a porta de origem pode ser outra — por isso a placa usa a porta de origem, e não 5005 fixo.
Só pacotes **válidos** atualizam esse endereço. (Mudança de 11/10/2026: antes o IP do PC ficava no
firmware.)

**E8. `OLA`.** Se a placa **já conhece** o endereço do PC (recebeu algo dele antes, ou o firmware
tem um IP de PC opcional), ela envia `OLA` ao ligar e ao (re)conectar no Wi-Fi, e repete a cada
5 s até receber uma mensagem do PC; logo após o `OLA`, envia também `MODO;<atual>`. Se não conhece,
não envia nada: espera o `PING`. Quando passa a conhecer um PC (endereço de origem novo), a placa
envia `MODO;<atual>` a ele. O PC, ao passar de "desconectada" para "conectada" (primeiro
`PONG`), envia `HORA`, `CAM` e o alerta ativo — então o `OLA` não é obrigatório.

**E9. PING/PONG.** O PC envia `PING;<seq>` a cada 2 s, incrementando `<seq>`. A placa responde
`PONG` com o **mesmo** `<seq>`. Qualquer `PONG` recebido nos últimos 6 s mantém o status
"conectada".

**E10. Botões.** Botão A: ver E14.
`BTN;B`: a placa alterna NORMAL/NOITE localmente e envia `BTN;B` seguido de `MODO;<novo>`.
`BTN;J`: só o PC decide a próxima câmera; se não houver câmera com PTZ, responde `CAM;-`.

**E11. Robustez da leitura.** Quem recebe aceita a mensagem com ou sem `\n` final e ignora
`\r`. Descarta (e registra) datagramas com mais de 64 bytes, bytes fora do ASCII
imprimível, comando desconhecido, número errado de campos ou valores fora da faixa.

**E12. Tela de espera.** Enquanto não recebe `PING` do PC por 6 s (ao ligar, ou se o app fechar),
a placa mostra no OLED "AGUARDANDO PC", o próprio IP e a porta — é dali que a pessoa copia o IP
para o app. Um alerta que esteja na tela continua até expirar (E2).

**E13. `SOM;<UMA|CONTINUO>` (12/10/2026).** Como o buzzer toca (Ajustes → Placa BitDogLab,
`placa.buzzer`). `UMA` (padrão da placa ao ligar): só quando chega um alerta NOVO (E1).
`CONTINUO`: o padrão do nível se repete — ALTO a cada 1,5 s, MÉDIO a cada 2,5 s — enquanto o
alerta estiver na placa (até `LIMPAR`, ou até expirar, E2). O PC manda `SOM` ao conectar, no
`OLA` e quando o ajuste muda. Qualquer outro valor é inválido (E11).

**E14. Botão A: 1 toque × 2 toques (12/10/2026, pedido do Diego).** A placa espera 0,4 s
depois de um toque para saber se vem o segundo.
- **1 toque**: liga/desliga o **mudo** da placa (nenhum som até outro toque). É só na placa:
  nada é enviado ao PC. O display mostra "MUDO" / "SEM SOM".
- **2 toques**: cala o **alerta atual** (um alerta NOVO volta a tocar) e envia `BTN;A`. O PC
  continua reenviando esse alerta, para ele seguir no display até o objeto sair (`LIMPAR`).
- Sem som também no modo NOITE (botão B).

## 7. Exemplos de conversa

Primeira vez (a placa não conhece o PC; OLED: "AGUARDANDO PC · IP 192.168.1.60 · PORTA 5006"),
a pessoa cadastra 192.168.1.60 no app:
```
PC → Placa   PING;0               (de 192.168.1.101:5005 para 192.168.1.60:5006)
Placa → PC   PONG;0               (para 192.168.1.101:5005 — a origem do PING)
PC → Placa   HORA;21:35           (o PC conectou: manda HORA, CAM e o alerta ativo)
PC → Placa   CAM;FRENTE
```

Placa reinicia com o PC já rodando (ela lembra o endereço do PC):
```
Placa → PC   OLA
Placa → PC   MODO;NORMAL
PC → Placa   HORA;21:35
PC → Placa   CAM;FRENTE
PC → Placa   PING;0
Placa → PC   PONG;0
```

Pessoa para em frente ao portão, usuário cala o alerta (2 toques no A), pessoa vai embora:
```
PC → Placa   ALERTA;FRENTE;PESSOA;8;0.87;7;ALTO     (buzzer: 3 bipes; com SOM;CONTINUO, repete)
PC → Placa   ALERTA;FRENTE;PESSOA;10;0.88;7;ALTO    (reenvio: só atualiza o OLED)
Placa → PC   BTN;A                                  (2 toques: o buzzer cala; o PC continua reenviando)
PC → Placa   ALERTA;FRENTE;PESSOA;12;0.88;7;ALTO    (o alerta segue no display, sem som)
PC → Placa   LIMPAR;FRENTE;7
PC → Placa   LIMPAR;FRENTE;7                        (1 s depois; ignorado)
```

Mover a câmera e trocar de câmera:
```
Placa → PC   JOY;0.75;0.00        (PC: ContinuousMove para a direita)
Placa → PC   JOY;0.75;0.00        (repetição a cada 1 s enquanto segura)
Placa → PC   JOY;0.00;0.00        (PC: Stop)
Placa → PC   BTN;J
PC → Placa   CAM;GARAGEM
```

## 8. Notas para o firmware em C

- **Maior mensagem possível** (PC → placa):
  `ALERTA;` (7) + CAM (10) + `;` + `DESCONHECIDO` (12) + `;` + seg (5) + `;` + `1.00` (4) + `;`
  + id (6) + `;` + `MEDIO` (5) + `\n` = **55 bytes** — cabe nos 64 com folga.
  Buffer de recepção recomendado: `char buf[65]` (64 + `\0`).
- Separe os campos com `strtok_r` (ou um laço procurando `;`) sobre uma cópia do buffer;
  converta números com `strtol`/`strtof` e confira o ponteiro final para validar.
- Monte as mensagens com `snprintf(buf, sizeof buf, "JOY;%.2f;%.2f\n", x, y)`; confira o retorno
  (< 64) antes de enviar.
- No Pico W (lwIP), receba com `udp_recv` em um PCB ligado à porta 5006. O callback recebe
  `addr` e `port` de origem: guarde os dois (só depois de validar a mensagem) e envie tudo com
  `udp_sendto(pcb, p, &addr_pc, porta_pc)`. Não faça trabalho pesado dentro do callback: copie a
  mensagem para uma fila e trate no laço principal.
- O IP da placa para o OLED: `ip4addr_ntoa(netif_ip4_addr(netif_list))` depois de conectar no Wi-Fi.
  A Pico W só usa Wi-Fi de **2,4 GHz** e não funciona em "rede de convidados" (isola os aparelhos).

## 9. Comportamento esperado da placa (referência para o firmware e o simulador)

- **OLED (128x64)**: sem `PING` do PC há 6 s mostra "AGUARDANDO PC", o IP da placa e a porta
  (E12). Em repouso mostra "MONITORANDO", a hora (`HORA`), a câmera do joystick (`CAM`), o último
  `PASSOU` e o IP da placa. Em alerta mostra o código da câmera, a classe em destaque e
  "PARADO 12s".
- **Matriz 5x5**: um ícone por classe; vermelho piscando = ALTO, amarelo = MEDIO,
  verde = BAIXO, azul = DESCONHECIDO.
- **Buzzer**: ALTO = 3 bipes rápidos; MEDIO = 1 bipe longo; BAIXO = sem som. Com `SOM;CONTINUO`,
  repete até o alerta acabar (E13). Modo NOITE e mudo = sem som.
- **Botões**: A = 1 toque liga/desliga o mudo, 2 toques calam o alerta atual (E14); B = trocar modo
  NORMAL/NOITE; J (botão do joystick) = próxima câmera com PTZ.
