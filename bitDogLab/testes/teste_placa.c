/*
 * teste_placa.c — testes da lógica da placa (placa.c), rodando no PC.
 *
 * Compilado e executado por testes/rodar_testes.py. Cada teste monta uma situação e confere
 * as mensagens que a placa mandaria ao PC (docs/protocolo.md).
 */
#include <stdio.h>
#include <string.h>

#include "../placa.h"

static int falhas = 0, verificacoes = 0;

#define CONFERIR(cond)                                                                 \
    do {                                                                               \
        verificacoes++;                                                                \
        if (!(cond)) {                                                                 \
            falhas++;                                                                  \
            printf("  FALHOU (%s:%d): %s\n", __FILE__, __LINE__, #cond);               \
        }                                                                              \
    } while (0)

static saida_t s;
static char erro[80];

static bool receber(placa_t *p, const char *texto, uint32_t agora) {
    saida_limpar(&s);
    return placa_receber(p, texto, strlen(texto), agora, &s, erro, sizeof erro);
}

static bool enviou(const char *msg) {
    for (int i = 0; i < s.n; i++) {
        if (strcmp(s.msgs[i], msg) == 0) {
            return true;
        }
    }
    return false;
}

static void teste_ping_pong_e_conexao(void) {
    placa_t p;
    placa_iniciar(&p, false);
    CONFERIR(!placa_pc_conectado(&p, 1000));
    CONFERIR(receber(&p, "PING;42\n", 1000));
    CONFERIR(s.n == 1 && enviou("PONG;42"));
    CONFERIR(placa_pc_conectado(&p, 7000));   /* 6 s depois ainda conta */
    CONFERIR(!placa_pc_conectado(&p, 7001));  /* E12: volta a "AGUARDANDO PC" */
    CONFERIR(receber(&p, "PING;007", 8000) && enviou("PONG;7")); /* sem \n e com zeros: vale */
    CONFERIR(!receber(&p, "PING;65536\n", 8000));
}

static void teste_mensagens_invalidas_sao_ignoradas(void) {
    placa_t p;
    placa_iniciar(&p, false);
    const char *ruins[] = {
        "", "\n", "ping;1\n", "PING\n", "PING;1;2\n", "PING;-1\n", "PING;1.5\n", "XYZ;1\n",
        "HORA;24:00\n", "HORA;9:05\n", "CAM;frente\n", "CAM;12345678901\n", "CAM;__\n",
        "ALERTA;FRENTE;PESSOA;12;0.87;7\n", "ALERTA;FRENTE;HUMANO;12;0.87;7;ALTO\n",
        "ALERTA;FRENTE;PESSOA;12;1.01;7;ALTO\n", "ALERTA;FRENTE;PESSOA;12;0.8;7;ALTO\n",
        "ALERTA;FRENTE;PESSOA;12;0.87;0;ALTO\n", "ALERTA;FRENTE;PESSOA;100000;0.87;7;ALTO\n",
        "LIMPAR;FRENTE\n", "PASSOU;FRENTE;AVIAO\n", "PING;1\n\n", "PING;\t1\n",
        "PING;11111111111111111111111111111111111111111111111111111111111111\n", /* 65 bytes */
    };
    for (size_t i = 0; i < sizeof ruins / sizeof ruins[0]; i++) {
        bool ok = receber(&p, ruins[i], 1000);
        if (ok) {
            printf("  aceitou indevidamente: %s\n", ruins[i]);
        }
        CONFERIR(!ok && s.n == 0);
    }
    char nao_ascii[] = "CAM;FRENTE\xC3\n";
    saida_limpar(&s);
    CONFERIR(!placa_receber(&p, nao_ascii, strlen(nao_ascii), 1000, &s, erro, sizeof erro));
    CONFERIR(receber(&p, "HORA;21:35\r\n", 1000) && strcmp(p.hora, "21:35") == 0); /* \r é ignorado */
    CONFERIR(receber(&p, "CAM;-\n", 1000) && strcmp(p.cam, "-") == 0);           /* nenhuma câmera */
}

static void teste_alerta_reenvio_limpar_e_expiracao(void) {
    placa_t p;
    placa_iniciar(&p, false);
    CONFERIR(receber(&p, "ALERTA;FRENTE;PESSOA;8;0.87;7;ALTO\n", 1000));
    CONFERIR(p.alerta.ativo && s.toque == TOQUE_ALTO);                     /* novo: 3 bipes */
    CONFERIR(placa_segundos_parado(&p, 3500) == 10);                       /* 8 + 2,5 s */
    CONFERIR(receber(&p, "ALERTA;FRENTE;PESSOA;10;0.88;7;ALTO\n", 3000));
    CONFERIR(s.toque == TOQUE_NENHUM && p.alerta.segundos == 10);          /* E1: reenvio não toca */
    CONFERIR(receber(&p, "LIMPAR;FRENTE;8\n", 3500) && p.alerta.ativo);    /* E3: outro id: ignora */
    CONFERIR(receber(&p, "LIMPAR;FRENTE;7\n", 4000) && !p.alerta.ativo);
    CONFERIR(receber(&p, "LIMPAR;FRENTE;7\n", 5000) && !p.alerta.ativo);   /* 2º LIMPAR: nada */

    CONFERIR(receber(&p, "ALERTA;GARAGEM;CARRO;3;0.70;9;MEDIO\n", 6000) && s.toque == TOQUE_MEDIO);
    saida_limpar(&s);
    placa_tique(&p, 12000, &s);
    CONFERIR(p.alerta.ativo);                                              /* 6 s exatos: ainda */
    placa_tique(&p, 12001, &s);
    CONFERIR(!p.alerta.ativo);                                             /* E2: expirou */

    CONFERIR(receber(&p, "ALERTA;FRENTE;GATO;1;0.50;3;BAIXO\n", 13000) && s.toque == TOQUE_NENHUM);
}

static void teste_modo_noite_e_silenciar(void) {
    placa_t p;
    placa_iniciar(&p, false);
    saida_limpar(&s);
    placa_apertar(&p, 'B', &s);
    CONFERIR(p.modo_noite && s.n == 2 && enviou("BTN;B") && enviou("MODO;NOITE"));
    CONFERIR(receber(&p, "ALERTA;FRENTE;PESSOA;8;0.87;7;ALTO\n", 1000) && s.toque == TOQUE_NENHUM);
    saida_limpar(&s);
    placa_apertar(&p, 'B', &s);
    CONFERIR(!p.modo_noite && enviou("MODO;NORMAL"));
    saida_limpar(&s);
    placa_apertar(&p, 'D', &s);                                            /* 2 toques no A */
    CONFERIR(p.buzzer_silenciado && s.n == 1 && enviou("BTN;A"));
    CONFERIR(receber(&p, "ALERTA;FRENTE;CARRO;2;0.90;8;ALTO\n", 2000) && s.toque == TOQUE_ALTO);
    CONFERIR(!p.buzzer_silenciado);                                        /* alerta novo: som volta */
    saida_limpar(&s);
    placa_apertar(&p, 'J', &s);
    CONFERIR(s.n == 1 && enviou("BTN;J"));
}

static void teste_botao_a_mudo_e_som_continuo(void) {
    placa_t p;
    placa_iniciar(&p, false);
    saida_limpar(&s);
    placa_apertar(&p, 'A', &s);                                            /* 1 toque: mudo, nada vai ao PC */
    CONFERIR(p.mudo && s.n == 0);
    CONFERIR(receber(&p, "ALERTA;FRENTE;PESSOA;8;0.87;7;ALTO\n", 1000) && s.toque == TOQUE_NENHUM);
    CONFERIR(!placa_som_permitido(&p));
    placa_apertar(&p, 'A', &s);                                            /* outro toque: volta ao normal */
    CONFERIR(!p.mudo && placa_som_permitido(&p));
    CONFERIR(placa_toque_continuo(&p) == TOQUE_NENHUM);                    /* padrão: só toca uma vez */
    CONFERIR(receber(&p, "SOM;CONTINUO\n", 1100) && p.som_continuo);
    CONFERIR(placa_toque_continuo(&p) == TOQUE_ALTO);                      /* toca até o alerta acabar */
    saida_limpar(&s);
    placa_apertar(&p, 'D', &s);
    CONFERIR(placa_toque_continuo(&p) == TOQUE_NENHUM && enviou("BTN;A")); /* 2 toques: calou este alerta */
    CONFERIR(receber(&p, "ALERTA;FRENTE;CARRO;3;0.70;9;MEDIO\n", 1200) && s.toque == TOQUE_MEDIO);
    CONFERIR(placa_toque_continuo(&p) == TOQUE_MEDIO);                     /* alerta novo: volta a tocar */
    CONFERIR(receber(&p, "LIMPAR;FRENTE;9\n", 1300) && placa_toque_continuo(&p) == TOQUE_NENHUM);
    CONFERIR(!receber(&p, "SOM;SEMPRE\n", 1400) && p.som_continuo);       /* inválido: ignora */
    CONFERIR(receber(&p, "SOM;UMA\n", 1500) && !p.som_continuo);
}

static void teste_joystick(void) {
    placa_t p;
    placa_iniciar(&p, false);
    saida_limpar(&s);
    placa_mover_joystick(&p, 5, -11, 1000, &s);                            /* zona morta */
    CONFERIR(s.n == 0);
    placa_mover_joystick(&p, 75, 0, 1000, &s);
    CONFERIR(s.n == 1 && enviou("JOY;0.75;0.00"));
    saida_limpar(&s);
    placa_mover_joystick(&p, 75, 0, 1010, &s);                             /* igual: não manda */
    CONFERIR(s.n == 0);
    placa_mover_joystick(&p, 80, 0, 1020, &s);                             /* 20 ms depois: segura */
    CONFERIR(s.n == 0 && p.joy_pendente);
    placa_tique(&p, 1049, &s);
    CONFERIR(s.n == 0);
    placa_tique(&p, 1050, &s);                                             /* 50 ms: vai */
    CONFERIR(s.n == 1 && enviou("JOY;0.80;0.00"));
    saida_limpar(&s);
    placa_tique(&p, 2049, &s);
    CONFERIR(s.n == 0);
    placa_tique(&p, 2050, &s);                                             /* E5: repete a cada 1 s */
    CONFERIR(s.n == 1 && enviou("JOY;0.80;0.00"));
    saida_limpar(&s);
    placa_mover_joystick(&p, -130, 100, 2200, &s);                         /* limita em -1 */
    CONFERIR(enviou("JOY;-1.00;1.00"));
    saida_limpar(&s);
    placa_mover_joystick(&p, 3, 2, 2300, &s);                              /* soltou: 0.00;0.00 1 vez */
    CONFERIR(s.n == 1 && enviou("JOY;0.00;0.00"));
    saida_limpar(&s);
    placa_tique(&p, 5000, &s);
    CONFERIR(s.n == 0);                                                    /* parado: não repete */
}

static void teste_ola_quando_conhece_o_pc(void) {
    placa_t p;
    placa_iniciar(&p, true);
    saida_limpar(&s);
    placa_ligar(&p, 1000, &s);
    CONFERIR(s.n == 2 && strcmp(s.msgs[0], "OLA") == 0 && strcmp(s.msgs[1], "MODO;NORMAL") == 0);
    saida_limpar(&s);
    placa_tique(&p, 5999, &s);
    CONFERIR(s.n == 0);
    placa_tique(&p, 6000, &s);                                             /* E8: repete a cada 5 s */
    CONFERIR(s.n == 2 && enviou("OLA"));
    CONFERIR(receber(&p, "HORA;21:35\n", 6500));
    saida_limpar(&s);
    placa_tique(&p, 20000, &s);
    CONFERIR(s.n == 0);                                                    /* o PC respondeu: chega de OLA */

    placa_t q;
    placa_iniciar(&q, false);
    saida_limpar(&s);
    placa_tique(&q, 9000, &s);
    CONFERIR(s.n == 0);                                                    /* não conhece: espera o PING */
    placa_pc_encontrado(&q, &s);
    CONFERIR(q.conhece_pc && s.n == 1 && enviou("MODO;NORMAL"));
}

static void teste_matriz(void) {
    placa_t p;
    uint32_t cores[5][5];
    placa_iniciar(&p, false);
    placa_matriz(&p, 1000, cores);
    CONFERIR(cores[0][2] == 0);
    receber(&p, "ALERTA;FRENTE;PESSOA;8;0.87;7;ALTO\n", 1000);
    placa_matriz(&p, 1000, cores);
    CONFERIR(cores[0][2] == 0xEF4444u && cores[0][0] == 0);                /* cabeça da pessoa, vermelha */
    placa_matriz(&p, 1250, cores);
    CONFERIR(cores[0][2] == 0);                                            /* ALTO pisca */
    receber(&p, "ALERTA;FRENTE;DESCONHECIDO;8;0.40;9;MEDIO\n", 2000);
    placa_matriz(&p, 2250, cores);
    CONFERIR(cores[0][1] == 0x3B82F6u);                                    /* DESCONHECIDO = azul, não pisca */
}

int main(void) {
    struct { const char *nome; void (*f)(void); } testes[] = {
        {"ping/pong e conexao", teste_ping_pong_e_conexao},
        {"mensagens invalidas sao ignoradas", teste_mensagens_invalidas_sao_ignoradas},
        {"alerta: reenvio, limpar e expiracao", teste_alerta_reenvio_limpar_e_expiracao},
        {"modo noite e silenciar", teste_modo_noite_e_silenciar},
        {"botao A: mudo, 2 toques e som continuo", teste_botao_a_mudo_e_som_continuo},
        {"joystick", teste_joystick},
        {"OLA quando conhece o PC", teste_ola_quando_conhece_o_pc},
        {"matriz", teste_matriz},
    };
    for (size_t i = 0; i < sizeof testes / sizeof testes[0]; i++) {
        int antes = falhas;
        testes[i].f();
        printf("%s %s\n", falhas == antes ? "[ok]   " : "[FALHA]", testes[i].nome);
    }
    printf("%d verificacoes, %d falha(s)\n", verificacoes, falhas);
    return falhas == 0 ? 0 : 1;
}
