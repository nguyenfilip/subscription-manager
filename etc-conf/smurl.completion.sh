#!/bin/bash

# To generate new api completions, run the gen-api-completions.py script

# If set, choose a different set of completions.
# 'hosted' vs 'test' or 'v1.0' vs 'v2.0', etc
SMURL_URL=${SMURL_URL:-default}

_read_api()
{
    local GET_METHODS
    local POST_METHODS
    local HEAD_HETHODS
    local PUT_METHODS
    local DELETE_METHODS
    local completions_dir="${HOME}/.smurl/completions/${SMURL_URL}"
    if [[ -d "${completions_dir}" ]] ; then
       mapfile GET_METHODS < "${completions_dir}/candlepin-api-completions-GET"
       mapfile POST_METHODS < "${completions_dir}/candlepin-api-completions-POST"
       mapfile PUT_METHODS < "${completions_dir}/candlepin-api-completions-PUT"
       mapfile DELETE_METHODS < "${completions_dir}/candlepin-api-completions-DELETE"
       mapfile HEAD_METHODS < "${completions_dir}/candlepin-api-completions-HEAD"
    fi
    ALL_METHODS="${GET_METHODS[*]} ${POST_METHODS[*]} ${PUT_METHODS[*]} ${DELETE_METHODS[*]} ${HEAD_METHODS[*]}"
}

_smurl_api()
{
    # we can/should check comp_words for the http verb, and filter to
    # a subset of the methods...
    #echo "sdfasdf ${ALL_METHODS[*]}"
    #echo "comp_words ${COMP_WORDS[*]} prev ${prev} cur ${cur}"
    local opts="--method --auth -d -X --request --username --password --org"
    local all_comp="${opts} ${ALL_METHODS}"
    COMPREPLY=($(compgen -W "${all_comp}" -- ${1}))
}

_smurl()
{
  local first cur prev opts base
  COMPREPLY=()

  first=${COMP_WORDS[1]}
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"
  opts="api
        get post put delete head patch
        --method --auth -d -X --request --username --password --org"

  case "${prev}" in
     -X|--request)
        local REQUEST_TYPES="GET POST PUT DELETE HEAD"
        COMPREPLY=($(compgen -W "${REQUEST_TYPES}" -- ${cur}))
        return 0
        ;;
    --auth)
        local AUTH_TYPES="consumer user none"
        COMPREPLY=( $(compgen -W "${AUTH_TYPES}" -- ${cur}) )
        return 0
        ;;
    -d)
        local filename="${1#@}"
        COMPREPLY=( $( compgen -W -o filenames "@- " -- "${filename}" ) )
        return 0
        ;;
    api|--method)
         "_smurl_api" "${cur}" "${prev}"
         return 0
         ;;
   esac

  case "${cur}" in
      --*)
          local OPTIONS="--method --auth -d -X --request --username --password --org"
          COMPREPLY=($(compgen -W "${OPTIONS}" -- ${cur}))
          # expand options
          return 0
          ;;
          # also need to complete get, etc, and handle get something_to_expand
          # if not a subcommand or option, try to expand method paths\
      *get|put|post|head|delete|patch)
          "_smurl_api" "${cur}" "${prev}"
          return 0
          ;;
      *)
         "_smurl_api" "${cur}" "${prev}"
         return 0
         ;;
   esac

  method_and_opts="${opts} ${METHODS}"
  COMPREPLY=($(compgen -W "${method_and_opts}" -- ${cur}))
  return 0

}

_read_api
complete -F _smurl -o default smurl
