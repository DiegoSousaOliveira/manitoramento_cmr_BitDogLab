"""Testes da lógica do firmware NO PC (sem gravar na placa).

    python testes/rodar_testes.py            (na pasta bitDogLab, com a .venv do projeto ativa)

1. Compila ``placa.c`` + ``teste_placa.c`` com o compilador C do Visual Studio (Build Tools) e
   roda os testes de unidade.
2. Compila ``placa_cli.c`` e manda MILHARES de eventos aleatórios (mensagens válidas montadas pelo
   próprio app — ``pc/link/protocolo.py`` — e mensagens estragadas de propósito, botões, joystick,
   passagem do tempo) para o firmware em C E para o simulador em Python
   (``pc/tools/simulador_bitdoglab.py``). As respostas precisam ser IDÊNTICAS: o simulador é a
   referência do comportamento da placa.
3. Confere que o app aceita (``interpretar``) toda mensagem que o firmware manda.
"""

from __future__ import annotations

import importlib.util
import random
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PASTA = Path(__file__).resolve().parent.parent  # bitDogLab/
PC = PASTA.parent / "pc"
BIN = PASTA / "testes" / "bin"
VSWHERE = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe")

CAMS = ["FRENTE", "GARAGEM", "CAM-1", "A_B", "7"]
CLASSES = ["PESSOA", "BICICLETA", "CARRO", "MOTO", "ONIBUS", "CAMINHAO", "GATO", "CACHORRO", "DESCONHECIDO"]
NIVEIS = ["BAIXO", "MEDIO", "ALTO"]
PASSOS_MS = [0, 1, 10, 30, 49, 50, 51, 120, 500, 999, 1000, 1001, 2000, 4999, 5000, 5001, 6000, 6001, 9000]


# ===================================================================== compilar
def achar_vcvars() -> Path:
    if VSWHERE.exists():
        saida = subprocess.run([str(VSWHERE), "-latest", "-products", "*", "-find",
                                r"VC\Auxiliary\Build\vcvars64.bat"], capture_output=True, text=True, check=False)
        for linha in saida.stdout.splitlines():
            if linha.strip():
                return Path(linha.strip())
    for raiz in (Path(r"C:\Program Files (x86)\Microsoft Visual Studio"), Path(r"C:\Program Files\Microsoft Visual Studio")):
        for achado in raiz.glob("*/*/VC/Auxiliary/Build/vcvars64.bat"):
            return achado
    raise SystemExit("Compilador C do Visual Studio não encontrado (instale o 'Build Tools for Visual Studio').")


def compilar(nome: str, fontes: list[str]) -> Path:
    BIN.mkdir(parents=True, exist_ok=True)
    exe = BIN / f"{nome}.exe"
    arquivos = " ".join(f'"{PASTA / f}"' for f in fontes)
    # Um .bat: o cmd não entende as aspas escapadas que o subprocess gera numa linha só.
    script = BIN / f"compilar_{nome}.bat"
    script.write_text(f'@call "{achar_vcvars()}" >nul\r\n'
                      f'@cl /nologo /W4 /WX /std:c11 /D_CRT_SECURE_NO_WARNINGS /Fe"{exe}" {arquivos}\r\n',
                      encoding="mbcs")
    resultado = subprocess.run(["cmd", "/c", str(script)], capture_output=True, text=True, check=False, cwd=BIN,
                               encoding="mbcs", errors="replace")
    if resultado.returncode != 0:
        print(resultado.stdout + resultado.stderr)
        raise SystemExit(f"Falha ao compilar {nome}")
    return exe


# ===================================================================== eventos aleatórios
def mensagens_do_app(rnd: random.Random) -> bytes:
    """Uma mensagem VÁLIDA, montada pelo mesmo código que o app usa."""
    from link import protocolo

    tipo = rnd.choice(["alerta", "alerta", "limpar", "passou", "camera", "ping", "hora", "som"])
    cam = rnd.choice(CAMS)
    if tipo == "alerta":
        texto = protocolo.alerta(cam, rnd.choice(CLASSES), rnd.uniform(0, 300), rnd.random(), rnd.randint(1, 5),
                                 rnd.choice(NIVEIS))
    elif tipo == "limpar":
        texto = protocolo.limpar(cam, rnd.randint(1, 5))
    elif tipo == "passou":
        texto = protocolo.passou(cam, rnd.choice(CLASSES))
    elif tipo == "camera":
        texto = protocolo.camera(rnd.choice([*CAMS, None]))
    elif tipo == "ping":
        texto = protocolo.ping(rnd.randint(0, 70000))
    elif tipo == "som":
        texto = protocolo.som(rnd.random() < 0.5)
    else:
        texto = protocolo.hora(datetime(2026, 10, 12, rnd.randint(0, 23), rnd.randint(0, 59)))
    return texto.encode("ascii")


def estragar(rnd: random.Random, dados: bytes) -> bytes:
    """Variações inválidas (ou quase) de uma mensagem boa: a placa precisa recusar igual ao simulador."""
    texto = dados.decode("ascii")
    jeito = rnd.randrange(14)
    if jeito == 0:
        return texto.lower().encode()
    if jeito == 1:
        return texto.rstrip("\n").encode()  # sem \n: vale (E11)
    if jeito == 2:
        return texto.replace("\n", "\r\n").encode()  # \r é ignorado
    if jeito == 3:
        return (texto + "\n").encode()  # dois \n: inválida
    if jeito == 4:
        campos = texto.rstrip("\n").split(";")
        return (";".join(campos[:-1]) + "\n").encode()  # falta um campo
    if jeito == 5:
        return (texto.rstrip("\n") + ";X\n").encode()  # campo a mais
    if jeito == 6:
        campos = texto.rstrip("\n").split(";")
        if len(campos) > 1:
            campos[rnd.randrange(1, len(campos))] = rnd.choice(["", "-1", "1.5", "abc", "99999999999", "007", "1.01",
                                                                  "0.5", "24:00", "--", "__", "Z", "00:00"])
        return (";".join(campos) + "\n").encode()
    if jeito == 7:
        return dados[:-1] + bytes([rnd.randrange(128, 256)]) + b"\n"  # fora do ASCII
    if jeito == 8:
        return (texto.rstrip("\n") + "0" * rnd.randint(40, 70) + "\n").encode()[:rnd.randint(60, 80)]
    if jeito == 9:
        return bytes(rnd.randrange(256) for _ in range(rnd.randint(0, 20)))
    if jeito == 10:
        return rnd.choice([b"", b"\n", b";", b";;;;", b"OLA\n", b"JOY;0.00;0.00\n", b"PING;1;\n", b"PING\t;1\n"])
    if jeito == 11:
        return texto.replace(";", ";" * rnd.randint(1, 2), 1).encode()
    if jeito == 12:
        i = rnd.randrange(len(dados))
        return dados[:i] + bytes([rnd.choice(b"\x00\x07\t\x1b\x7f")]) + dados[i + 1:]
    return dados


def gerar_eventos(rnd: random.Random, quantidade: int) -> list[tuple]:
    eventos: list[tuple] = [("I", rnd.random() < 0.5)]
    agora = rnd.randint(0, 3000)
    for _ in range(quantidade):
        agora += rnd.choice(PASSOS_MS)
        sorteio = rnd.random()
        if sorteio < 0.40:
            dados = mensagens_do_app(rnd)
            if rnd.random() < 0.35:
                dados = estragar(rnd, dados)
            eventos.append(("R", agora, dados))
        elif sorteio < 0.62:
            eventos.append(("T", agora))
        elif sorteio < 0.82:
            x = rnd.choice([0, 0, 5, -11, 12, -12, 50, -75, 100, 130, -140, rnd.randint(-110, 110)])
            y = rnd.choice([0, 0, 3, 13, -50, 80, -100, rnd.randint(-110, 110)])
            eventos.append(("J", agora, x, y))
        elif sorteio < 0.94:
            eventos.append(("B", rnd.choice("AABJDD")))
        elif sorteio < 0.97:
            eventos.append(("E",))
        else:
            eventos.append(("L", agora))
    return eventos


def para_texto(evento: tuple) -> str:
    tipo = evento[0]
    if tipo == "I":
        return f"I {int(evento[1])}"
    if tipo in ("L", "T"):
        return f"{tipo} {evento[1]}"
    if tipo == "R":
        return f"R {evento[1]} {evento[2].hex()}"
    if tipo == "B":
        return f"B {evento[1]}"
    if tipo == "J":
        return f"J {evento[1]} {evento[2]} {evento[3]}"
    return "E"


# ===================================================================== o mesmo no simulador
def carregar_simulador():  # noqa: ANN201
    spec = importlib.util.spec_from_file_location("simulador_bitdoglab", PC / "tools" / "simulador_bitdoglab.py")
    modulo = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = modulo  # as dataclasses do simulador procuram o módulo aqui
    spec.loader.exec_module(modulo)
    # O firmware conta o tempo em MILISSEGUNDOS: o simulador recebe ms e os tempos em ms.
    for nome, valor in {"MIN_INTERVALO_JOY_S": 50, "REPETIR_JOY_S": 1000, "REPETIR_OLA_S": 5000,
                        "EXPIRAR_ALERTA_S": 6000, "SEM_PC_S": 6000}.items():
        setattr(modulo, nome, valor)
    return modulo


def rodar_simulador(sim, eventos: list[tuple]) -> list[str]:  # noqa: ANN001
    placa = sim.PlacaSimulada()
    agora = 0
    linhas: list[str] = []
    for evento in eventos:
        tipo = evento[0]
        saida: list[str] = []
        toque = None
        invalida = False
        if tipo == "I":
            placa = sim.PlacaSimulada()
            placa.conhece_pc = bool(evento[1])
        elif tipo == "L":
            agora = evento[1]
            saida = placa.ligar(agora)
        elif tipo == "E":
            saida = placa.pc_encontrado()
        elif tipo == "T":
            agora = evento[1]
            saida = placa.tique(agora)
        elif tipo == "R":
            agora = evento[1]
            try:
                resposta = placa.receber(evento[2], agora)
                saida, toque = resposta.enviar, resposta.buzzer
            except sim.ErroMensagem:
                invalida = True
        elif tipo == "B":
            saida = placa.apertar("AA" if evento[1] == "D" else evento[1])  # D = 2 toques no A
        elif tipo == "J":
            agora = evento[1]
            saida = placa.mover_joystick(evento[2] / 100, evento[3] / 100, agora)
        linhas += [f"S {m}" for m in saida]
        if toque:
            linhas.append(f"Z {toque}")
        if invalida:
            linhas.append("X")
        a = placa.alerta
        alerta = f"{a.cam},{a.classe},{a.segundos},{round(a.confianca * 100)},{a.id},{a.nivel}" if a else "-"
        linhas.append(f"= alerta={alerta} hora={placa.hora} cam={placa.cam} passou={placa.passou} modo={placa.modo} "
                      f"sil={int(placa.buzzer_silenciado)} resp={int(placa.pc_respondeu)} "
                      f"conhece={int(placa.conhece_pc)} conectado={int(placa.pc_conectado(agora))} "
                      f"mudo={int(placa.mudo)} som={int(placa.som_continuo)} toque={placa.toque_continuo() or '-'}")
        cores = [c[1:] if c else "000000" for linha in placa.matriz(agora / 1000) for c in linha]
        linhas.append("M " + " ".join(cores))
        linhas.append(".")
    return linhas


# ===================================================================== principal
def agrupar(linhas: list[str]) -> list[list[str]]:
    """As linhas de cada evento (cada resposta termina com ".")."""
    grupos: list[list[str]] = []
    atual: list[str] = []
    for linha in linhas:
        if linha == ".":
            grupos.append(atual)
            atual = []
        else:
            atual.append(linha)
    return grupos


def main() -> int:
    sys.path.insert(0, str(PC))  # para importar link.protocolo (o código do app)
    from link.protocolo import ErroProtocolo, interpretar

    print("1) Testes de unidade (placa.c)")
    teste = compilar("teste_placa", ["testes/teste_placa.c", "placa.c"])
    if subprocess.run([str(teste)], check=False).returncode != 0:
        return 1

    print("\n2) Firmware x simulador (mesmos eventos, respostas idênticas)")
    cli = compilar("placa_cli", ["testes/placa_cli.c", "placa.c"])
    sim = carregar_simulador()
    total_eventos = total_msgs = invalidas = toques = 0
    tipos: set[str] = set()
    for semente in range(40):
        rnd = random.Random(semente)
        eventos = gerar_eventos(rnd, 1500)
        entrada = "\n".join(para_texto(e) for e in eventos) + "\n"
        firmware = subprocess.run([str(cli)], input=entrada, capture_output=True, text=True, check=True).stdout.splitlines()
        esperado = rodar_simulador(sim, eventos)
        if firmware != esperado:  # mostra o primeiro evento em que os dois discordaram
            for i, (f, s) in enumerate(zip(agrupar(firmware), agrupar(esperado), strict=False)):
                if f != s:
                    print(f"  DIFERENÇA (semente {semente}, evento {i}): {para_texto(eventos[i])}")
                    print(f"    firmware : {f}\n    simulador: {s}")
                    break
            return 1
        enviadas = [linha[2:] for linha in firmware if linha.startswith("S ")]
        for mensagem in enviadas:  # 3) o app entende tudo o que a placa manda
            try:
                interpretar((mensagem + "\n").encode("ascii"))
            except ErroProtocolo as erro:
                print(f"  O app recusaria a mensagem do firmware {mensagem!r}: {erro}")
                return 1
        total_eventos += len(eventos)
        total_msgs += len(enviadas)
        invalidas += firmware.count("X")
        toques += sum(1 for linha in firmware if linha.startswith("Z "))
        tipos |= {m.split(";")[0] for m in enviadas}
    print(f"[ok]    {total_eventos} eventos: firmware e simulador responderam igual "
          f"({invalidas} mensagens recusadas, {toques} toques do buzzer)")
    print(f"[ok]    o app aceitou as {total_msgs} mensagens que o firmware mandou ({', '.join(sorted(tipos))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
