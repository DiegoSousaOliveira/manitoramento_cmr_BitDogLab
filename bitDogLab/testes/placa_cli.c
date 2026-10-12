/*
 * placa_cli.c — a lógica da placa (placa.c) controlada por linhas de texto, para comparar com o
 * simulador em Python (testes/rodar_testes.py manda os MESMOS eventos para os dois).
 *
 * Entrada (uma por linha):          Saída (para cada entrada):
 *   I <0|1>       iniciar             S <mensagem>   cada mensagem para o PC
 *   L <ms>        ligar (OLA)         Z <ALTO|MEDIO> o buzzer tocaria
 *   E             PC encontrado       X              mensagem do PC inválida (ignorada)
 *   T <ms>        tique               = <estado>     resumo do estado
 *   R <ms> <hex>  mensagem do PC      M <25 cores>   matriz de LEDs
 *   B <A|D|B|J>   botão (D = 2 toques no A)   .     fim da resposta
 *   J <ms> <x> <y> joystick (centésimos)
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../placa.h"

static int hex(char c) {
    return (c >= '0' && c <= '9') ? c - '0' : (c >= 'a' && c <= 'f') ? c - 'a' + 10 : (c >= 'A' && c <= 'F') ? c - 'A' + 10 : -1;
}

int main(void) {
    placa_t p;
    saida_t s;
    char linha[512], erro[80];
    uint32_t agora = 0;
    placa_iniciar(&p, false);
    setvbuf(stdout, NULL, _IOFBF, 1 << 16);
    while (fgets(linha, sizeof linha, stdin)) {
        saida_limpar(&s);
        bool invalida = false;
        char tipo = linha[0];
        if (tipo == 'I') {
            placa_iniciar(&p, atoi(linha + 2) != 0);
        } else if (tipo == 'L') {
            agora = (uint32_t)strtoul(linha + 2, NULL, 10);
            placa_ligar(&p, agora, &s);
        } else if (tipo == 'E') {
            placa_pc_encontrado(&p, &s);
        } else if (tipo == 'T') {
            agora = (uint32_t)strtoul(linha + 2, NULL, 10);
            placa_tique(&p, agora, &s);
        } else if (tipo == 'R') {
            char *resto;
            agora = (uint32_t)strtoul(linha + 2, &resto, 10);
            while (*resto == ' ') resto++;
            char dados[256];
            size_t n = 0;
            while (hex(resto[0]) >= 0 && hex(resto[1]) >= 0 && n < sizeof dados) {
                dados[n++] = (char)(hex(resto[0]) * 16 + hex(resto[1]));
                resto += 2;
            }
            invalida = !placa_receber(&p, dados, n, agora, &s, erro, sizeof erro);
        } else if (tipo == 'B') {
            placa_apertar(&p, linha[2], &s);
        } else if (tipo == 'J') {
            int x, y;
            unsigned long ms;
            sscanf(linha + 2, "%lu %d %d", &ms, &x, &y);
            agora = (uint32_t)ms;
            placa_mover_joystick(&p, x, y, agora, &s);
        }
        for (int i = 0; i < s.n; i++) {
            printf("S %s\n", s.msgs[i]);
        }
        if (s.toque != TOQUE_NENHUM) {
            printf("Z %s\n", s.toque == TOQUE_ALTO ? "ALTO" : "MEDIO");
        }
        if (invalida) {
            printf("X\n");
        }
        if (p.alerta.ativo) {
            printf("= alerta=%s,%s,%u,%u,%u,%s", p.alerta.cam, nome_classe(p.alerta.classe),
                   (unsigned)p.alerta.segundos, (unsigned)p.alerta.confianca, (unsigned)p.alerta.id,
                   nome_nivel(p.alerta.nivel));
        } else {
            printf("= alerta=-");
        }
        toque_t continuo = placa_toque_continuo(&p);
        printf(" hora=%s cam=%s passou=%s modo=%s sil=%d resp=%d conhece=%d conectado=%d mudo=%d som=%d toque=%s\n",
               p.hora, p.cam, p.passou, p.modo_noite ? "NOITE" : "NORMAL", p.buzzer_silenciado, p.pc_respondeu,
               p.conhece_pc, placa_pc_conectado(&p, agora), p.mudo, p.som_continuo,
               continuo == TOQUE_ALTO ? "ALTO" : continuo == TOQUE_MEDIO ? "MEDIO" : "-");
        uint32_t cores[5][5];
        placa_matriz(&p, agora, cores);
        printf("M");
        for (int l = 0; l < 5; l++) {
            for (int c = 0; c < 5; c++) {
                printf(" %06X", (unsigned)cores[l][c]);
            }
        }
        printf("\n.\n");
    }
    return 0;
}
