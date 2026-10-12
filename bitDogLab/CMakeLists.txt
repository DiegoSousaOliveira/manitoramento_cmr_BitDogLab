# Generated Cmake Pico project file

cmake_minimum_required(VERSION 3.13)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Initialise pico_sdk from installed location
# (note this can come from environment, CMake cache etc)

# == DO NOT EDIT THE FOLLOWING LINES for the Raspberry Pi Pico VS Code Extension to work ==
if(WIN32)
    set(USERHOME $ENV{USERPROFILE})
else()
    set(USERHOME $ENV{HOME})
endif()
set(sdkVersion 2.3.1)
set(toolchainVersion 15_2_Rel1)
set(picotoolVersion 2.3.1)
set(picoVscode ${USERHOME}/.pico-sdk/cmake/pico-vscode.cmake)
if (EXISTS ${picoVscode})
    include(${picoVscode})
endif()
# ====================================================================================
set(PICO_BOARD pico_w CACHE STRING "Board type")

# Pull in Raspberry Pi Pico SDK (must be before project)
include(pico_sdk_import.cmake)

project(bitDogLab C CXX ASM)

# Initialise the Raspberry Pi Pico SDK
pico_sdk_init()

# Nome e senha do Wi-Fi: wifi_config.h (fora do Git). Na primeira vez, é criado a partir do modelo
# e a placa mostra "FALTA O WI-FI" até você preencher.
if(NOT EXISTS ${CMAKE_CURRENT_LIST_DIR}/wifi_config.h)
    configure_file(${CMAKE_CURRENT_LIST_DIR}/wifi_config.exemplo.h ${CMAKE_CURRENT_LIST_DIR}/wifi_config.h COPYONLY)
    message(WARNING "Criei bitDogLab/wifi_config.h a partir do modelo: coloque nele o nome e a senha do Wi-Fi.")
endif()

# O programa principal (main.c) e os módulos da placa
add_executable(bitDogLab
        main.c
        placa.c
        rede.c
        oled.c
        telas.c
        matriz.c
        buzzer.c
        entradas.c
        )

# Avisos do compilador ligados só nos NOSSOS arquivos (os do SDK teriam muitos)
set_source_files_properties(main.c placa.c rede.c oled.c telas.c matriz.c buzzer.c entradas.c
        PROPERTIES COMPILE_OPTIONS "-Wall;-Wextra")

pico_set_program_name(bitDogLab "bitDogLab")
pico_set_program_version(bitDogLab "1.0")
pico_set_program_description(bitDogLab "Sentinela Casa - placa de alertas e joystick")

# Matriz de LEDs WS2812: o sinal é gerado pelo PIO (programa do próprio SDK)
pico_generate_pio_header(bitDogLab ${CMAKE_CURRENT_LIST_DIR}/ws2812.pio)

# Mensagens de diagnóstico pela USB (porta serial)
pico_enable_stdio_uart(bitDogLab 0)
pico_enable_stdio_usb(bitDogLab 1)

target_compile_definitions(bitDogLab PRIVATE
        CYW43_HOST_NAME="BitDogLab-Sentinela"   # nome da placa na lista do roteador
        )

target_link_libraries(bitDogLab
        pico_stdlib
        hardware_adc
        hardware_i2c
        hardware_pio
        hardware_pwm
        hardware_watchdog
        pico_cyw43_arch_lwip_threadsafe_background
        )

# lwipopts.h (configuração da pilha de rede) e wifi_config.h ficam nesta pasta
target_include_directories(bitDogLab PRIVATE
        ${CMAKE_CURRENT_LIST_DIR}
)

pico_add_extra_outputs(bitDogLab)
