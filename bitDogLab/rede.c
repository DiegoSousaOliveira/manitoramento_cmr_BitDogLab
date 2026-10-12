/*
 * rede.c — Wi-Fi e UDP com o lwIP (pilha de rede da Pico W).
 *
 * O lwIP roda "em segundo plano" (interrupções do chip CYW43). Quando chega um pacote, a função
 * ao_receber() é chamada nesse contexto: ela só COPIA a mensagem para uma fila; o laço principal
 * tira da fila e trata (nota 8 do protocolo: nada pesado dentro do callback).
 * Tudo o que mexe no lwIP fora dele fica entre cyw43_arch_lwip_begin() e _end() (a "trava").
 */
#include "rede.h"

#include <stdio.h>
#include <string.h>

#include "lwip/pbuf.h"
#include "lwip/udp.h"
#include "pico/cyw43_arch.h"
#include "wifi_config.h"

#define TAMANHO_FILA      16
#define LIMITE_CONEXAO_MS 30000 /* sem IP em 30 s: tenta de novo */
#define ESPERA_FALHA_MS   5000  /* depois de uma falha, espera antes de tentar de novo */

static struct udp_pcb *pcb = NULL;
static pacote_t fila[TAMANHO_FILA];
static int inicio_fila = 0, tamanho_fila = 0;

static estado_wifi_t estado = WIFI_CONECTANDO;
static const char *motivo = "";
static int tentativas = 0;
static uint32_t desde = 0;
static bool precisa_conectar = true, conectou_agora = false;
static char ip[16] = "";

/* ------------------------------------------------------------ UDP */
static void ao_receber(void *arg, struct udp_pcb *p_pcb, struct pbuf *p, const ip_addr_t *addr, u16_t port) {
    (void)arg;
    (void)p_pcb;
    if (p == NULL) {
        return;
    }
    if (tamanho_fila < TAMANHO_FILA) {
        pacote_t *novo = &fila[(inicio_fila + tamanho_fila) % TAMANHO_FILA];
        uint16_t copiar = p->tot_len > TAMANHO_MAXIMO + 1 ? TAMANHO_MAXIMO + 1 : p->tot_len;
        pbuf_copy_partial(p, novo->dados, copiar, 0);
        novo->dados[copiar] = '\0';
        novo->n = p->tot_len; /* o tamanho REAL: maior que 64 é recusado depois (E11) */
        ip_addr_copy(novo->origem, *addr);
        novo->porta = port;
        tamanho_fila++;
    } /* fila cheia: descarta (UDP pode perder; o PC reenvia o que importa) */
    pbuf_free(p);
}

bool rede_receber(pacote_t *pacote) {
    bool tem = false;
    cyw43_arch_lwip_begin();
    if (tamanho_fila > 0) {
        *pacote = fila[inicio_fila];
        inicio_fila = (inicio_fila + 1) % TAMANHO_FILA;
        tamanho_fila--;
        tem = true;
    }
    cyw43_arch_lwip_end();
    return tem;
}

bool rede_enviar(const char *texto, const ip_addr_t *destino, uint16_t porta) {
    char linha[TAMANHO_MAXIMO + 1];
    int n = snprintf(linha, sizeof linha, "%s\n", texto);
    if (n <= 0 || n > TAMANHO_MAXIMO || estado != WIFI_CONECTADO) {
        return false;
    }
    bool ok = false;
    cyw43_arch_lwip_begin();
    struct pbuf *p = pbuf_alloc(PBUF_TRANSPORT, (u16_t)n, PBUF_RAM);
    if (p != NULL) {
        memcpy(p->payload, linha, (size_t)n);
        ok = udp_sendto(pcb, p, destino, porta) == ERR_OK;
        pbuf_free(p);
    }
    cyw43_arch_lwip_end();
    return ok;
}

/* ------------------------------------------------------------ Wi-Fi */
bool rede_iniciar(void) {
    if (cyw43_arch_init_with_country(CYW43_COUNTRY_BRAZIL) != 0) {
        motivo = "chip do Wi-Fi falhou";
        estado = WIFI_FALHOU;
        return false;
    }
    cyw43_arch_enable_sta_mode();
    /* Sem economia de energia: responde ao PING e ao joystick sem atraso (a placa fica na tomada). */
    cyw43_wifi_pm(&cyw43_state, CYW43_NONE_PM);
    cyw43_arch_lwip_begin();
    pcb = udp_new_ip_type(IPADDR_TYPE_V4);
    bool ok = pcb != NULL && udp_bind(pcb, IP_ANY_TYPE, PORTA_PLACA) == ERR_OK;
    if (ok) {
        udp_recv(pcb, ao_receber, NULL);
    }
    cyw43_arch_lwip_end();
    if (!ok) {
        motivo = "porta UDP falhou";
        estado = WIFI_FALHOU;
        return false;
    }
    if (strlen(WIFI_NOME) == 0 || strcmp(WIFI_NOME, "NOME_DA_SUA_REDE") == 0) {
        estado = WIFI_SEM_CONFIGURACAO; /* wifi_config.h ainda com o exemplo */
    }
    return true;
}

static void comecar_conexao(uint32_t agora) {
    if (tentativas > 0) {
        cyw43_wifi_leave(&cyw43_state, CYW43_ITF_STA); /* sai do estado anterior antes de tentar de novo */
    }
    tentativas++;
    desde = agora;
    estado = WIFI_CONECTANDO;
    motivo = "";
    ip[0] = '\0';
    printf("Wi-Fi: conectando em \"%s\" (tentativa %d)\n", WIFI_NOME, tentativas);
    if (cyw43_arch_wifi_connect_async(WIFI_NOME, WIFI_SENHA, CYW43_AUTH_WPA2_AES_PSK) != 0) {
        estado = WIFI_FALHOU;
        motivo = "erro ao iniciar";
    }
    precisa_conectar = false;
}

void rede_atualizar(uint32_t agora) {
    if (estado == WIFI_SEM_CONFIGURACAO) {
        return;
    }
    if (precisa_conectar) {
        comecar_conexao(agora);
        return;
    }
    int status = cyw43_tcpip_link_status(&cyw43_state, CYW43_ITF_STA);
    if (estado == WIFI_CONECTANDO) {
        if (status == CYW43_LINK_UP) {
            estado = WIFI_CONECTADO;
            conectou_agora = true;
            snprintf(ip, sizeof ip, "%s", ip4addr_ntoa(netif_ip4_addr(&cyw43_state.netif[CYW43_ITF_STA])));
            printf("Wi-Fi: conectado. IP da placa: %s, porta %d\n", ip, PORTA_PLACA);
        } else {
            if (status == CYW43_LINK_BADAUTH) {
                motivo = "senha errada?";
            } else if (status == CYW43_LINK_NONET) {
                motivo = "rede nao achada";
            } else if (status == CYW43_LINK_FAIL) {
                motivo = "falha ao entrar";
            } else if (agora - desde > LIMITE_CONEXAO_MS) {
                motivo = "demorou demais";
            }
            if (motivo[0] != '\0') {
                estado = WIFI_FALHOU;
                desde = agora;
                printf("Wi-Fi: falhou (%s)\n", motivo);
            }
        }
    } else if (estado == WIFI_FALHOU) {
        if (agora - desde > ESPERA_FALHA_MS) {
            precisa_conectar = true; /* tenta de novo */
        }
    } else if (status != CYW43_LINK_UP) { /* estava conectado e caiu */
        printf("Wi-Fi: conexao perdida (status %d)\n", status);
        motivo = "conexao perdida";
        precisa_conectar = true;
        ip[0] = '\0';
        estado = WIFI_CONECTANDO;
    }
}

estado_wifi_t rede_estado(void) { return estado; }
const char *rede_motivo(void) { return motivo; }
int rede_tentativas(void) { return tentativas; }
const char *rede_ip(void) { return ip; }
const char *rede_nome(void) { return WIFI_NOME; }

bool rede_conectou_agora(void) {
    bool foi = conectou_agora;
    conectou_agora = false;
    return foi;
}
