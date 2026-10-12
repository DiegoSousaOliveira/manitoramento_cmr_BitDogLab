/*
 * placa.c — lógica da placa (protocolo + estado). Ver placa.h.
 *
 * Tradução direta da classe PlacaSimulada do simulador em Python: mesma ordem de validação,
 * mesmas respostas. Se mudar algo aqui, mude lá também (e rode testes/rodar_testes.py, que
 * compara os dois com milhares de mensagens).
 */
#include "placa.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

static const char *const NOMES_CLASSES[N_CLASSES] = {
    "PESSOA", "BICICLETA", "CARRO", "MOTO", "ONIBUS", "CAMINHAO", "GATO", "CACHORRO", "DESCONHECIDO",
};
static const char *const NOMES_NIVEIS[3] = {"BAIXO", "MEDIO", "ALTO"};

/* Ícones 5x5 ('#' = aceso), os mesmos do simulador. */
static const char *const ICONES[N_CLASSES][5] = {
    {"..#..", ".###.", "..#..", ".#.#.", ".#.#."}, /* PESSOA */
    {".....", "..#..", ".#.#.", "#.#.#", ".#.#."}, /* BICICLETA */
    {".....", ".###.", "#####", "#####", ".#.#."}, /* CARRO */
    {".....", "...##", ".###.", "#.#.#", ".#.#."}, /* MOTO */
    {"#####", "#.#.#", "#####", "#####", ".#.#."}, /* ONIBUS */
    {"###..", "####.", "#####", "#####", ".#.#."}, /* CAMINHAO */
    {"#...#", "##.##", "#####", ".###.", "..#.."}, /* GATO */
    {"#....", "###.#", ".####", ".#..#", ".#..#"}, /* CACHORRO */
    {".###.", "#...#", "..##.", ".....", "..#.."}, /* DESCONHECIDO (um "?") */
};
#define COR_ALTO         0xEF4444u /* vermelho */
#define COR_MEDIO        0xF5B300u /* amarelo */
#define COR_BAIXO        0x22C55Eu /* verde */
#define COR_DESCONHECIDO 0x3B82F6u /* azul */

const char *nome_classe(classe_t c) { return (unsigned)c < N_CLASSES ? NOMES_CLASSES[c] : "?"; }
const char *nome_nivel(nivel_t n) { return (unsigned)n <= NIVEL_ALTO ? NOMES_NIVEIS[n] : "?"; }

/* ===================================================================== saídas */
void saida_limpar(saida_t *s) {
    s->n = 0;
    s->toque = TOQUE_NENHUM;
}

static void enviar(saida_t *s, const char *formato, ...) {
    if (s->n >= MAX_SAIDAS) {
        return; /* não acontece (no máximo 3 por chamada), mas nunca estoura o vetor */
    }
    va_list args;
    va_start(args, formato);
    int tamanho = vsnprintf(s->msgs[s->n], sizeof s->msgs[s->n], formato, args);
    va_end(args);
    if (tamanho > 0 && tamanho < TAMANHO_MAXIMO) { /* + o \n final cabe nos 64 bytes */
        s->n++;
    }
}

/* Centésimos -> "0.75", "-1.00" (sem printf de float: igual ao f"{x:.2f}" do Python). */
static void centesimos(char *texto, size_t tam, int valor) {
    int absoluto = valor < 0 ? -valor : valor;
    snprintf(texto, tam, "%s%d.%02d", valor < 0 ? "-" : "", absoluto / 100, absoluto % 100);
}

/* ===================================================================== validação (E11) */
static void explicar(char *erro, size_t tam, const char *formato, ...) {
    if (erro == NULL || tam == 0) {
        return;
    }
    va_list args;
    va_start(args, formato);
    vsnprintf(erro, tam, formato, args);
    va_end(args);
}

/* "^\d+$" e min <= valor <= max (zeros à esquerda valem: "007" = 7, como no Python). */
static bool inteiro(const char *t, uint32_t min, uint32_t max, uint32_t *valor) {
    if (*t == '\0') {
        return false;
    }
    uint64_t v = 0;
    for (const char *c = t; *c; c++) {
        if (*c < '0' || *c > '9') {
            return false;
        }
        v = v * 10 + (uint64_t)(*c - '0');
        if (v > 4000000000ull) {
            v = 4000000000ull; /* satura: continua "grande demais" sem estourar */
        }
    }
    if (v < min || v > max) {
        return false;
    }
    *valor = (uint32_t)v;
    return true;
}

/* Código de câmera: 1–10 de [A-Z0-9_-], com ao menos uma letra ou dígito ("-" sozinho opcional). */
static bool codigo(const char *t, bool aceita_nenhuma) {
    if (aceita_nenhuma && strcmp(t, "-") == 0) {
        return true;
    }
    size_t n = strlen(t);
    if (n < 1 || n > TAM_CODIGO) {
        return false;
    }
    bool tem_alfanumerico = false;
    for (size_t i = 0; i < n; i++) {
        char c = t[i];
        bool alfanumerico = (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9');
        if (!alfanumerico && c != '_' && c != '-') {
            return false;
        }
        tem_alfanumerico |= alfanumerico;
    }
    return tem_alfanumerico;
}

static int achar(const char *t, const char *const *nomes, int quantos) {
    for (int i = 0; i < quantos; i++) {
        if (strcmp(t, nomes[i]) == 0) {
            return i;
        }
    }
    return -1;
}

/* "^[01]\.\d{2}$" e no máximo 1.00 -> centésimos. */
static bool confianca(const char *t, uint32_t *valor) {
    if (strlen(t) != 4 || (t[0] != '0' && t[0] != '1') || t[1] != '.' || t[2] < '0' || t[2] > '9' ||
        t[3] < '0' || t[3] > '9') {
        return false;
    }
    *valor = (uint32_t)((t[0] - '0') * 100 + (t[2] - '0') * 10 + (t[3] - '0'));
    return *valor <= 100;
}

/* "^([01]\d|2[0-3]):[0-5]\d$" */
static bool hora_valida(const char *t) {
    if (strlen(t) != 5 || t[2] != ':') {
        return false;
    }
    bool hh = (t[0] >= '0' && t[0] <= '1' && t[1] >= '0' && t[1] <= '9') ||
              (t[0] == '2' && t[1] >= '0' && t[1] <= '3');
    bool mm = t[3] >= '0' && t[3] <= '5' && t[4] >= '0' && t[4] <= '9';
    return hh && mm;
}

/* ===================================================================== estado */
void placa_iniciar(placa_t *p, bool conhece_pc) {
    memset(p, 0, sizeof *p);
    p->conhece_pc = conhece_pc;
    strcpy(p->hora, "--:--");
    strcpy(p->cam, "-");
}

void placa_ligar(placa_t *p, uint32_t agora, saida_t *s) {
    p->ja_mandou_ola = true;
    p->ultimo_ola_ms = agora;
    enviar(s, "OLA");
    enviar(s, "MODO;%s", p->modo_noite ? "NOITE" : "NORMAL");
}

void placa_pc_encontrado(placa_t *p, saida_t *s) {
    p->conhece_pc = true;
    enviar(s, "MODO;%s", p->modo_noite ? "NOITE" : "NORMAL");
}

void placa_wifi_voltou(placa_t *p) {
    if (p->conhece_pc) {
        p->pc_respondeu = false; /* o próximo tique manda OLA + MODO */
        p->ja_mandou_ola = false;
    }
}

static void enviar_joy(placa_t *p, int x, int y, uint32_t agora, saida_t *s) {
    char tx[16], ty[16]; /* "-1.00" cabe com folga */
    p->joy_x = x;
    p->joy_y = y;
    p->joy_pendente = false;
    p->ja_mandou_joy = true;
    p->ultimo_joy_ms = agora;
    centesimos(tx, sizeof tx, x);
    centesimos(ty, sizeof ty, y);
    enviar(s, "JOY;%s;%s", tx, ty);
}

void placa_tique(placa_t *p, uint32_t agora, saida_t *s) {
    if (p->conhece_pc && !p->pc_respondeu && (!p->ja_mandou_ola || agora - p->ultimo_ola_ms >= REPETIR_OLA_MS)) {
        placa_ligar(p, agora, s);
    }
    if (p->alerta.ativo && agora - p->alerta.recebido_ms > EXPIRAR_ALERTA_MS) {
        p->alerta.ativo = false; /* E2: o PC parou de reenviar (silenciado ou LIMPAR perdido) */
    }
    bool pode = !p->ja_mandou_joy || agora - p->ultimo_joy_ms >= MIN_INTERVALO_JOY_MS;
    if (p->joy_pendente && pode) {
        enviar_joy(p, p->pend_x, p->pend_y, agora, s);
    } else if ((p->joy_x != 0 || p->joy_y != 0) && agora - p->ultimo_joy_ms >= REPETIR_JOY_MS) {
        enviar_joy(p, p->joy_x, p->joy_y, agora, s); /* E5: segurando, repete a cada 1 s */
    }
}

/* ===================================================================== mensagens do PC */
#define MAX_CAMPOS 8

bool placa_receber(placa_t *p, const char *dados, size_t n, uint32_t agora, saida_t *s,
                   char *erro, size_t tam_erro) {
    if (n > TAMANHO_MAXIMO) {
        explicar(erro, tam_erro, "%u bytes (maximo %d)", (unsigned)n, TAMANHO_MAXIMO);
        return false;
    }
    /* Copia sem os \r; o \n final é opcional (E11). */
    char texto[TAMANHO_MAXIMO + 1];
    size_t t = 0;
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)dados[i];
        if (c > 0x7F) {
            explicar(erro, tam_erro, "bytes fora do ASCII");
            return false;
        }
        if (c != '\r') {
            texto[t++] = (char)c;
        }
    }
    if (t > 0 && texto[t - 1] == '\n') {
        t--;
    }
    texto[t] = '\0';
    if (t == 0) {
        explicar(erro, tam_erro, "vazia");
        return false;
    }
    for (size_t i = 0; i < t; i++) {
        if ((unsigned char)texto[i] < 0x20 || (unsigned char)texto[i] > 0x7E) {
            explicar(erro, tam_erro, "caracteres de controle");
            return false;
        }
    }

    /* Separa os campos por ';' (campos vazios contam, como o split do Python). */
    char *campos[MAX_CAMPOS];
    int n_campos = 0;
    campos[n_campos++] = texto;
    for (size_t i = 0; i < t; i++) {
        if (texto[i] == ';') {
            if (n_campos == MAX_CAMPOS) {
                explicar(erro, tam_erro, "campos demais");
                return false;
            }
            texto[i] = '\0';
            campos[n_campos++] = &texto[i + 1];
        }
    }
    const char *comando = campos[0];
    char **args = &campos[1];
    int n_args = n_campos - 1;

    static const struct { const char *nome; int campos; } COMANDOS[] = {
        {"ALERTA", 6}, {"LIMPAR", 2}, {"PASSOU", 2}, {"CAM", 1}, {"PING", 1}, {"HORA", 1}, {"SOM", 1},
    };
    int esperado = -1;
    for (size_t i = 0; i < sizeof COMANDOS / sizeof COMANDOS[0]; i++) {
        if (strcmp(comando, COMANDOS[i].nome) == 0) {
            esperado = COMANDOS[i].campos;
        }
    }
    if (esperado < 0) {
        explicar(erro, tam_erro, "comando desconhecido: %s", comando);
        return false;
    }
    if (n_args != esperado) {
        explicar(erro, tam_erro, "%s precisa de %d campo(s), veio %d", comando, esperado, n_args);
        return false;
    }
    p->pc_respondeu = true;

    if (strcmp(comando, "ALERTA") == 0) {
        int classe = achar(args[1], NOMES_CLASSES, N_CLASSES);
        int nivel = achar(args[5], NOMES_NIVEIS, 3);
        uint32_t conf, seg, id;
        if (classe < 0 || nivel < 0 || !confianca(args[3], &conf)) {
            explicar(erro, tam_erro, "ALERTA com classe/nivel/confianca invalidos");
            return false;
        }
        if (!codigo(args[0], false) || !inteiro(args[2], 0, 99999, &seg) || !inteiro(args[4], 1, 999999, &id)) {
            explicar(erro, tam_erro, "ALERTA com camera/segundos/id invalidos");
            return false;
        }
        bool mesmo = p->alerta.ativo && strcmp(p->alerta.cam, args[0]) == 0 && p->alerta.id == id;
        p->alerta.ativo = true;
        snprintf(p->alerta.cam, sizeof p->alerta.cam, "%s", args[0]);
        p->alerta.classe = (classe_t)classe;
        p->alerta.segundos = seg;
        p->alerta.confianca = conf;
        p->alerta.id = id;
        p->alerta.nivel = (nivel_t)nivel;
        p->alerta.recebido_ms = agora;
        if (!mesmo) { /* E1: reenvio só atualiza a tela; alerta NOVO toca o buzzer */
            p->buzzer_silenciado = false;
            if (!p->modo_noite && !p->mudo && nivel != NIVEL_BAIXO) {
                s->toque = nivel == NIVEL_ALTO ? TOQUE_ALTO : TOQUE_MEDIO;
            }
        }
    } else if (strcmp(comando, "LIMPAR") == 0) {
        uint32_t id;
        if (!codigo(args[0], false) || !inteiro(args[1], 1, 999999, &id)) {
            explicar(erro, tam_erro, "LIMPAR invalido");
            return false;
        }
        if (p->alerta.ativo && strcmp(p->alerta.cam, args[0]) == 0 && p->alerta.id == id) {
            p->alerta.ativo = false; /* E3: LIMPAR de outro objeto (ou repetido) é ignorado */
        }
    } else if (strcmp(comando, "PASSOU") == 0) {
        if (achar(args[1], NOMES_CLASSES, N_CLASSES) < 0 || !codigo(args[0], false)) {
            explicar(erro, tam_erro, "PASSOU invalido");
            return false;
        }
        snprintf(p->passou, sizeof p->passou, "%s %s", args[1], args[0]);
    } else if (strcmp(comando, "CAM") == 0) {
        if (!codigo(args[0], true)) {
            explicar(erro, tam_erro, "CAM invalido");
            return false;
        }
        snprintf(p->cam, sizeof p->cam, "%s", args[0]);
    } else if (strcmp(comando, "PING") == 0) {
        uint32_t seq;
        if (!inteiro(args[0], 0, 65535, &seq)) {
            explicar(erro, tam_erro, "PING invalido");
            return false;
        }
        p->tem_ping = true;
        p->ultimo_ping_ms = agora;
        enviar(s, "PONG;%u", (unsigned)seq);
    } else if (strcmp(comando, "SOM") == 0) { /* E13 */
        if (strcmp(args[0], "UMA") == 0) {
            p->som_continuo = false;
        } else if (strcmp(args[0], "CONTINUO") == 0) {
            p->som_continuo = true;
        } else {
            explicar(erro, tam_erro, "SOM invalido");
            return false;
        }
    } else { /* HORA */
        if (!hora_valida(args[0])) {
            explicar(erro, tam_erro, "hora invalida");
            return false;
        }
        snprintf(p->hora, sizeof p->hora, "%s", args[0]);
    }
    return true;
}

/* ===================================================================== botões e joystick */
void placa_apertar(placa_t *p, char botao, saida_t *s) {
    if (botao == 'A') { /* E14: 1 toque liga/desliga o MUDO — só na placa (o PC não precisa saber) */
        p->mudo = !p->mudo;
    } else if (botao == 'D') { /* E14: 2 toques calam o alerta ATUAL e avisam o PC */
        p->buzzer_silenciado = true;
        enviar(s, "BTN;A");
    } else if (botao == 'B') {
        p->modo_noite = !p->modo_noite;
        enviar(s, "BTN;B");
        enviar(s, "MODO;%s", p->modo_noite ? "NOITE" : "NORMAL");
    } else if (botao == 'J') {
        enviar(s, "BTN;J");
    }
}

static int eixo(int v) {
    if (v > 100) {
        v = 100;
    } else if (v < -100) {
        v = -100;
    }
    return (v < ZONA_MORTA && v > -ZONA_MORTA) ? 0 : v; /* E6: zona morta */
}

void placa_mover_joystick(placa_t *p, int x, int y, uint32_t agora, saida_t *s) {
    x = eixo(x);
    y = eixo(y);
    if (x == p->joy_x && y == p->joy_y) {
        p->joy_pendente = false;
        return;
    }
    if (p->ja_mandou_joy && agora - p->ultimo_joy_ms < MIN_INTERVALO_JOY_MS) {
        p->joy_pendente = true; /* rápido demais: vai no próximo tique (máx. 20/s) */
        p->pend_x = x;
        p->pend_y = y;
        return;
    }
    enviar_joy(p, x, y, agora, s);
}

/* ===================================================================== o que aparece */
bool placa_pc_conectado(const placa_t *p, uint32_t agora) {
    return p->tem_ping && agora - p->ultimo_ping_ms <= SEM_PC_MS;
}

bool placa_som_permitido(const placa_t *p) {
    return !p->mudo && !p->buzzer_silenciado && !p->modo_noite;
}

toque_t placa_toque_continuo(const placa_t *p) {
    if (!p->som_continuo || !p->alerta.ativo || !placa_som_permitido(p) || p->alerta.nivel == NIVEL_BAIXO) {
        return TOQUE_NENHUM;
    }
    return p->alerta.nivel == NIVEL_ALTO ? TOQUE_ALTO : TOQUE_MEDIO;
}

uint32_t placa_segundos_parado(const placa_t *p, uint32_t agora) {
    return p->alerta.segundos + (agora - p->alerta.recebido_ms) / 1000;
}

void placa_matriz(const placa_t *p, uint32_t agora, uint32_t cores[5][5]) {
    memset(cores, 0, sizeof(uint32_t) * 25);
    if (!p->alerta.ativo) {
        return;
    }
    if (p->alerta.nivel == NIVEL_ALTO && (agora / 250) % 2 == 1) {
        return; /* ALTO pisca 2x por segundo */
    }
    uint32_t cor = p->alerta.classe == CLASSE_DESCONHECIDO ? COR_DESCONHECIDO
                 : p->alerta.nivel == NIVEL_ALTO          ? COR_ALTO
                 : p->alerta.nivel == NIVEL_MEDIO         ? COR_MEDIO
                                                          : COR_BAIXO;
    for (int linha = 0; linha < 5; linha++) {
        for (int coluna = 0; coluna < 5; coluna++) {
            cores[linha][coluna] = ICONES[p->alerta.classe][linha][coluna] == '#' ? cor : 0;
        }
    }
}
