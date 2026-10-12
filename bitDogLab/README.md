# Firmware da placa BitDogLab — Sentinela Casa

Firmware em C (Pico SDK 2.3.1) para a **BitDogLab** (Raspberry Pi Pico W). A placa avisa
quando algo fica parado em frente à casa e move a câmera com o joystick. Ela conversa com o app
do PC por UDP, seguindo o protocolo de [docs/protocolo.md](../docs/protocolo.md).

| Peça da placa | O que faz |
| --- | --- |
| Display OLED 128x64 | "AGUARDANDO PC" + IP da placa · "MONITORANDO" + hora, câmera, último PASSOU · ALERTA com a classe e "PARADO 12s" |
| Matriz 5x5 de LEDs | ícone da classe: vermelho piscando = ALTO, amarelo = MEDIO, verde = BAIXO, azul = DESCONHECIDO |
| Buzzer | ALTO = 3 bipes rápidos, MEDIO = 1 bipe longo, BAIXO = sem som. Toca uma vez ou **até o alerta acabar** (Ajustes → Placa no app). Mudo e modo NOITE = sem som |
| Botão A | **1 toque**: liga/desliga o mudo · **2 toques rápidos**: cala só o alerta atual (ele fica no display até o objeto sair) |
| Botão B | troca o modo NORMAL / NOITE |
| Joystick | move a câmera (PTZ); **apertar a alavanca** = próxima câmera com motor |
| LED da Pico W | aceso = conversando com o PC |

## 0. O que você precisa

- Uma **BitDogLab** com Raspberry Pi Pico W (RP2040) e um cabo USB de dados. Sem a BitDogLab, uma
  Pico W com as mesmas peças ligadas nos mesmos pinos (tabela no fim) também serve.
- **VS Code** com a extensão **Raspberry Pi Pico** (`raspberry-pi.raspberry-pi-pico`). Ela baixa sozinha
  o que o projeto pede — Pico SDK **2.3.1**, compilador ARM **15_2_Rel1**, CMake, Ninja e picotool —
  para a pasta `~/.pico-sdk` (no Windows, `C:\Users\<você>\.pico-sdk`).
- Um Wi-Fi de **2,4 GHz** (WPA2), o mesmo que o PC consegue alcançar.

Abra a pasta `bitDogLab` no VS Code (**Arquivo → Abrir pasta**). A extensão reconhece o projeto pelo
`CMakeLists.txt`; se ela não oferecer a instalação do SDK, use **Raspberry Pi Pico → Import Project** e
escolha a pasta `bitDogLab`. As extensões recomendadas aparecem sozinhas (`.vscode/extensions.json`).

## 1. Wi-Fi (uma vez)

O nome e a senha da rede ficam em `bitDogLab/wifi_config.h`. Esse arquivo **não vai para o Git**
(está no `.gitignore`), então num clone novo ele ainda não existe. Crie-o a partir do modelo:

```bash
cd bitDogLab
cp wifi_config.exemplo.h wifi_config.h      # Windows (cmd): copy wifi_config.exemplo.h wifi_config.h
```

(Se você compilar antes, o CMake cria o arquivo sozinho, avisa no log e a placa mostra
**FALTA O WI-FI** no display.) Depois troque o nome e a senha:

```c
#define WIFI_NOME  "minha_rede"
#define WIFI_SENHA "minha_senha"
```

A Pico W só conecta em **2,4 GHz** (WPA2) e não funciona em "rede de convidados".

## 2. Compilar e gravar

Pela extensão **Raspberry Pi Pico** do VS Code: com a pasta `bitDogLab` aberta, use **Compile Project**.
O resultado é `build/bitDogLab.uf2`.

Pela linha de comando (Git Bash no Windows, ou terminal no Linux), depois que a extensão instalou as
ferramentas em `~/.pico-sdk`:

```bash
export PICO_SDK_PATH=~/.pico-sdk/sdk/2.3.1
export PATH=~/.pico-sdk/toolchain/15_2_Rel1/bin:~/.pico-sdk/cmake/v4.3.4/bin:~/.pico-sdk/ninja/v1.13.2:$PATH
cd bitDogLab
cmake -S . -B build -G Ninja
ninja -C build                 # gera build/bitDogLab.uf2
```

Depois, para gravar:

1. Desligue o cabo USB, **segure o botão BOOTSEL** da Pico W e ligue o cabo (solte em seguida).
   Aparece uma unidade chamada **RPI-RP2**.
2. Copie `build/bitDogLab.uf2` para essa unidade. A placa reinicia sozinha com o programa novo.
   (Ou use **Run Project** na extensão, que faz o mesmo com o `picotool`.)

Com a placa já rodando este firmware, não precisa do BOOTSEL: `picotool load build/bitDogLab.uf2 -f`
a reinicia em modo de gravação sozinho. Se ela continuar como **RPI-RP2** depois de gravar (aconteceu
com o `-x`), rode `picotool reboot` ou desligue e ligue o cabo.

## 3. Ligar no app

1. A placa entra no Wi-Fi e mostra **AGUARDANDO PC**, o **IP** e a **porta 5006**.
2. No app do PC: **Ajustes → Placa BitDogLab** → digite esse IP → **Salvar e conectar**.
3. Em até 2 s a placa mostra **MONITORANDO** e o LED da Pico W acende. O app mostra "Placa
   conectada" na barra lateral.

> **Não mexa no joystick enquanto a placa liga**: ela mede a posição de repouso nesse instante.

## 4. Testes no PC (sem gravar)

```bash
cd bitDogLab
python testes/rodar_testes.py
```

O script compila a lógica da placa (`placa.c`) com o compilador do Visual Studio e roda dois
testes. Ele funciona no **Windows** e precisa das **Build Tools do Visual Studio** com a carga de
trabalho "Desenvolvimento para desktop com C++" (o script acha o compilador sozinho pelo `vswhere`):

- **Testes de unidade** da lógica.
- **Comparação com o simulador:** 60 mil eventos aleatórios (mensagens do app, mensagens
  estragadas, botões, joystick, tempo passando) vão para o firmware **e** para o simulador em
  Python. As respostas precisam ser idênticas, e o app precisa aceitar tudo o que o firmware manda.

## 5. Ver o que a placa está fazendo

O firmware escreve um diário pela USB (porta serial COM, qualquer velocidade). Abra o
**Serial Monitor** da extensão, o PuTTY ou, sem instalar nada,
`python testes/ler_serial.py` (Windows; acha a porta da placa sozinho). Aparece algo assim:

```text
Wi-Fi: conectado. IP da placa: 192.168.0.57, porta 5006
PC encontrado em 192.168.0.2:5005
<- ALERTA;CAM01;PESSOA;8;0.87;7;ALTO
-> JOY;0.75;0.00 (adc x=3900 y=2041)
status: wifi=1 ip=192.168.0.57 pc=conectado alerta=- modo=NORMAL joy(adc x=2050 y=2038)
```

## Arquivos

| Arquivo | Papel |
| --- | --- |
| `main.c` | o programa principal: liga tudo e roda o laço (rede → entradas → lógica → buzzer, LEDs, display) |
| `placa.c/.h` | **a lógica** (protocolo + estado), sem hardware: a mesma do simulador, testada no PC |
| `rede.c/.h` | Wi-Fi (reconecta sozinho) e UDP na porta 5006, com uma fila entre a interrupção e o laço |
| `oled.c/.h`, `fonte5x7.h`, `telas.c/.h` | driver do display 128x64, fonte ASCII completa e as telas |
| `matriz.c/.h`, `ws2812.pio` | matriz de LEDs pelo PIO (programa do próprio SDK) |
| `buzzer.c/.h` | padrões de bipe por PWM, sem travar o laço |
| `entradas.c/.h` | botões (com filtro de repique) e joystick (centro medido ao ligar, passos de 0,05) |
| `config.h` | pinos, portas e tempos do protocolo |
| `lwipopts.h` | configuração da pilha de rede (lwIP). **O nome é obrigatório**: o lwIP procura este arquivo |
| `wifi_config.h` | nome e senha do Wi-Fi (fora do Git) |
| `wifi_config.exemplo.h` | modelo do `wifi_config.h` (este vai para o Git) |
| `.vscode/` | configuração da extensão Raspberry Pi Pico (compilar, gravar, depurar) |
| `testes/` | testes no PC (`rodar_testes.py`, `teste_placa.c`, `placa_cli.c`) |

## Pinos (BitDogLab)

| Peça | GPIO |
| --- | --- |
| Botões A / B | 5 / 6 |
| Joystick: eixo X / eixo Y / botão | 27 (ADC1) / 26 (ADC0) / 22 |
| Buzzer | 10 |
| Matriz 5x5 (WS2812) | 7 |
| OLED (I2C1, endereço 0x3C): SDA / SCL | 14 / 15 |

Se o joystick mandar a câmera para o lado errado, troque `JOY_INVERTER_X` ou `JOY_INVERTER_Y` em
`config.h`. Isso conserta só o joystick da placa. O app tem outro ajuste, em **Ajustes → PTZ →
Inverter**, que inverte o motor da câmera para qualquer comando.
