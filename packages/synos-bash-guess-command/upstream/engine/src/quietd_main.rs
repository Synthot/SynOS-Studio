fn main() {
    let mut arguments = std::env::args_os().skip(1);
    let result = match (arguments.next(), arguments.next(), arguments.next()) {
        (None, None, None) => synos_quiet_engine::runtime::serve_stdio(),
        (Some(flag), Some(path), None) if flag == "--fixture-bin-dir" => {
            synos_quiet_engine::runtime::serve_stdio_with_fixture_bin(path.into())
        }
        _ => {
            eprintln!("usage: synos-quietd [--fixture-bin-dir PATH]");
            std::process::exit(2);
        }
    };
    if let Err(error) = result {
        eprintln!("synos-quietd: {error}");
        std::process::exit(1);
    }
}
