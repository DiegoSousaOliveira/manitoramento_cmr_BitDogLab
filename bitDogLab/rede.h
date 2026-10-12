/*
 * rede.h — Wi-Fi (com reconexão automática) e UDP na porta 5006.
 */
#ifndef REDE_H
#define REDE_H

#include <stdbool.h>
#include <stdint.h>

#include "config.h"
#include "lwip/ip_addr.h"

typedef enum { WIFI_CONECTANDO, WIFI_CONECTADO, WIFI_FALHOU, WIFI_SEM_CONFIGURACAO } estado_wifi_t;

typedef struct {
    char dados[TAMANHO_MAXIMO + 2]; /* até 65 bytes + '\0' (só para imprimir no terminal) */
    uint16_t n;          /* bytes recebidos (se > TAMANHO_MAXIMO, só os primeiros foram guardados) */
    ip_addr_t origem;    /* de onde veio: a placa responde para cá (E7) */
    uint16_t porta;
} pacote_t;

bool rede_iniciar(void);             /* liga o chip do Wi-Fi e abre a porta UDP */
void rede_atualizar(uint32_t agora); /* chamar sempre: conecta, reconecta e detecta queda */
estado_wifi_t rede_estado(void);
const char *rede_motivo(void);       /* por que falhou (para o display) */
int rede_tentativas(void);
const char *rede_ip(void);           /* IP da placa ("" se desconectada) */
const char *rede_nome(void);         /* nome da rede (SSID) */
bool rede_conectou_agora(void);      /* true uma vez a cada (re)conexão */

bool rede_receber(pacote_t *pacote); /* tira o próximo pacote da fila */
bool rede_enviar(const char *texto, const ip_addr_t *destino, uint16_t porta); /* põe o \n */

#endif
