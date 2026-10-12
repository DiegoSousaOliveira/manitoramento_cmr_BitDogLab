/*
 * placa.h — a LÓGICA da placa: protocolo + estado, sem hardware e sem rede.
 *
 * É a tradução para C da classe PlacaSimulada (pc/tools/simulador_bitdoglab.py).
 * Por não depender de nada da Pico, compila também no PC: os testes em testes/
 * conferem que ela responde EXATAMENTE como o simulador às mesmas mensagens.
 *
 * Tempo: sempre em milissegundos ("agora"), passado por quem chama.
 * Joystick: em CENTÉSIMOS (-100..100) — 0,75 vira 75 — para não comparar floats.
 */
#ifndef PLACA_H
#define PLACA_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "config.h"

#define TAM_CODIGO 10   /* <CAM>: até 10 caracteres */
#define TAM_CLASSE 12   /* DESCONHECIDO */

typedef enum { NIVEL_BAIXO, NIVEL_MEDIO, NIVEL_ALTO } nivel_t;

typedef enum {
    CLASSE_PESSOA, CLASSE_BICICLETA, CLASSE_CARRO, CLASSE_MOTO, CLASSE_ONIBUS,
    CLASSE_CAMINHAO, CLASSE_GATO, CLASSE_CACHORRO, CLASSE_DESCONHECIDO, N_CLASSES
} classe_t;

/* O que o buzzer deve tocar ao chegar um alerta NOVO. */
typedef enum { TOQUE_NENHUM, TOQUE_ALTO, TOQUE_MEDIO } toque_t;

typedef struct {
    bool ativo;
    char cam[TAM_CODIGO + 1];
    classe_t classe;
    uint32_t segundos;      /* parado há quanto tempo, quando o PC mandou */
    uint32_t confianca;     /* em centésimos (0.87 -> 87) */
    uint32_t id;
    nivel_t nivel;
    uint32_t recebido_ms;   /* quando chegou o último ALERTA (novo ou reenvio) */
} alerta_t;

typedef struct {
    bool modo_noite;
    char hora[6];                       /* "21:35" ou "--:--" */
    char cam[TAM_CODIGO + 1];           /* câmera do joystick ("-" = nenhuma) */
    char passou[TAM_CLASSE + 1 + TAM_CODIGO + 1]; /* "CARRO FRENTE" ou "" */
    alerta_t alerta;
    bool buzzer_silenciado;             /* o alerta ATUAL foi calado (2 toques no A); um alerta novo toca */
    bool mudo;                          /* 1 toque no A: nenhum som até outro toque (só na placa) */
    bool som_continuo;                  /* E13: SOM;CONTINUO = toca até o alerta acabar */
    bool conhece_pc;                    /* sabe o endereço do PC (pode mandar OLA) */
    bool pc_respondeu;                  /* o PC já mandou algo válido (para de repetir OLA) */
    bool ja_mandou_ola;
    uint32_t ultimo_ola_ms;
    bool tem_ping;
    uint32_t ultimo_ping_ms;
    int joy_x, joy_y;                   /* último JOY enviado (centésimos) */
    bool joy_pendente;                  /* mudou rápido demais: vai no próximo tique */
    int pend_x, pend_y;
    bool ja_mandou_joy;
    uint32_t ultimo_joy_ms;
} placa_t;

/* Respostas de uma chamada: mensagens para enviar ao PC (sem o \n) e o toque do buzzer. */
#define MAX_SAIDAS 4
typedef struct {
    char msgs[MAX_SAIDAS][TAMANHO_MAXIMO + 1];
    int n;
    toque_t toque;
} saida_t;

void placa_iniciar(placa_t *p, bool conhece_pc);
void saida_limpar(saida_t *s);

/* Ao ligar (ou reconectar ao Wi-Fi) sabendo onde está o PC (E8): OLA e o modo atual. */
void placa_ligar(placa_t *p, uint32_t agora, saida_t *s);
/* Passou a conhecer um PC (endereço de origem novo): conta o modo a ele (E8). */
void placa_pc_encontrado(placa_t *p, saida_t *s);
/* O Wi-Fi voltou: se já conhece o PC, volta a mandar OLA (a cada 5 s) até ele responder (E8). */
void placa_wifi_voltou(placa_t *p);
/* Chamar várias vezes por segundo: OLA repetido, alerta expirado, joystick mantido. */
void placa_tique(placa_t *p, uint32_t agora, saida_t *s);

/* Mensagem do PC (bytes como chegaram). Devolve true se válida; se inválida, explica em "erro"
 * (regra E11: ignorar e registrar, nunca travar). */
bool placa_receber(placa_t *p, const char *dados, size_t n, uint32_t agora, saida_t *s,
                   char *erro, size_t tam_erro);

/* Botão apertado: 'A' (1 toque no A = mudo liga/desliga), 'D' (2 toques no A = cala o alerta atual),
 * 'B' ou 'J'. Quem distingue 1 de 2 toques é quem lê o botão (entradas.c). */
void placa_apertar(placa_t *p, char botao, saida_t *s);
/* Posição do joystick em centésimos (-100..100). Envia só quando muda, até 20/s (E5, E6). */
void placa_mover_joystick(placa_t *p, int x, int y, uint32_t agora, saida_t *s);

/* O PC mandou PING nos últimos 6 s? */
bool placa_pc_conectado(const placa_t *p, uint32_t agora);
/* O buzzer pode tocar agora? (não está no mudo, nem no modo NOITE, nem com o alerta calado) */
bool placa_som_permitido(const placa_t *p);
/* E13: com SOM;CONTINUO, o padrão que deve continuar tocando enquanto o alerta existir. */
toque_t placa_toque_continuo(const placa_t *p);
/* Segundos parado mostrados na tela: o do PC + o tempo desde que o alerta chegou. */
uint32_t placa_segundos_parado(const placa_t *p, uint32_t agora);

const char *nome_classe(classe_t c);
const char *nome_nivel(nivel_t n);

/* Matriz 5x5: cor de cada LED (0xRRGGBB, 0 = apagado), [linha][coluna] a partir do canto de cima
 * à esquerda. Um ícone por classe; ALTO pisca 2x por segundo. */
void placa_matriz(const placa_t *p, uint32_t agora, uint32_t cores[5][5]);

#endif
