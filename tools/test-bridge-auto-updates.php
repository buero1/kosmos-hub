<?php
define( 'ABSPATH', __DIR__ . '/' );
$options = array();
$filters = array();
$admin = true;
$multisite = false;
$fail_save = false;
$installed = array( 'one/one.php' => array(), 'two/two.php' => array(), 'other.php' => array() );
class WP_Error { public $code; public function __construct( $code, $message, $data = array() ) { $this->code = $code; } }
function __( $text, $domain = '' ) { return $text; }
function esc_html__( $text, $domain = '' ) { return htmlspecialchars( $text ); }
function current_user_can( $cap ) { return $GLOBALS['admin']; }
function is_multisite() { return $GLOBALS['multisite']; }
function get_plugins() { return $GLOBALS['installed']; }
function wp_register_ability( $name, $definition ) { $GLOBALS['abilities'][ $name ] = $definition; }
function get_site_option( $name, $default = array() ) { return $GLOBALS['options'][ $name ] ?? $default; }
function update_site_option( $name, $value ) {
	if ( $GLOBALS['fail_save'] ) { return false; }
	$GLOBALS['options'][ $name ] = apply_filters( 'pre_update_site_option_' . $name, $value );
	return true;
}
function add_filter( $name, $callback, $priority = 10, $args = 1 ) { $GLOBALS['filters'][ $name ][ $priority ][] = $callback; }
function apply_filters( $name, $value, ...$args ) {
	$filters = $GLOBALS['filters'][ $name ] ?? array();
	ksort( $filters );
	foreach ( $filters as $callbacks ) { foreach ( $callbacks as $callback ) { $value = $callback( $value, ...$args ); } }
	return $value;
}
spl_autoload_register( function ( $class ) {
	$prefix = 'KosmosBridge\\';
	if ( strpos( $class, $prefix ) === 0 ) { require_once __DIR__ . '/../wordpress-plugin/src/' . str_replace( '\\', '/', substr( $class, strlen( $prefix ) ) ) . '.php'; }
} );
function check( $value, $message ) { if ( ! $value ) { throw new Exception( $message ); } }
use KosmosBridge\Updates\AutoUpdatePolicy as Policy;
use KosmosBridge\Abilities\Registry;
Policy::boot();
$options['auto_update_plugins'] = array( 'one/one.php', 'two/two.php', 'other.php' );
$request = array( 'plugin_files' => array( 'one/one.php' ), 'blocked' => true );
$before = $options;
$admin = false;
check( Policy::write( $request ) instanceof WP_Error && $options === $before, 'Unauthorized call must not write' );
$admin = true;
$multisite = true;
check( Policy::write( $request ) instanceof WP_Error && $options === $before, 'Do not change entire multisite network' );
$multisite = false;
foreach ( array( array(), array( '../evil.php' ), array( 'one/one.php', false ), array( 'one/one.php', 'a\\b.php' ), array_fill( 0, 101, 'other.php' ) ) as $bad ) {
	check( Policy::write( array( 'plugin_files' => $bad, 'blocked' => true ) ) instanceof WP_Error && $options === $before, 'Validate all inputs before writing' );
}
check( Policy::write( array( 'plugin_files' => array( 'one/one.php' ), 'blocked' => 'false' ) ) instanceof WP_Error, 'Boolean must be strict' );
$result = Registry::execute_fallback_ability( Policy::WRITE_ABILITY, $request );
check( $result['plugins'][0]['verified'] && $result['plugins'][0]['blocked'], 'Block must be verified' );
check( $options['auto_update_plugins'] === array( 'two/two.php', 'other.php' ), 'Preserve unrelated auto updates' );
check( false === apply_filters( 'auto_update_plugin', true, (object) array( 'plugin' => 'one/one.php' ) ), 'Block background update' );
check( true === apply_filters( 'auto_update_plugin', true, (object) array( 'plugin' => 'other.php' ) ), 'Preserve security updates of other plugins' );
check( null === apply_filters( 'auto_update_plugin', null, (object) array( 'plugin' => 'other.php' ) ), 'Preserve default decision' );
update_site_option( 'auto_update_plugins', array( 'one/one.php', 'two/two.php', 'other.php' ) );
check( ! in_array( 'one/one.php', $options['auto_update_plugins'], true ), 'WP UI cannot accidentally undo Hub block' );
$before = $options;
Policy::write( $request );
check( $before === $options, 'Idempotent writes' );
$read = Registry::execute_fallback_ability( Policy::READ_ABILITY, array( 'plugin_files' => array( 'one/one.php' ) ) );
check( $read['plugins'][0]['blocked'] && $before === $options, 'Read must not mutate' );
$missing = Policy::write( array( 'plugin_files' => array( 'absent/absent.php' ), 'blocked' => true ) );
check( ! $missing['plugins'][0]['installed'] && $options === $before, 'Missing plugin is not changed' );
$released = Policy::write( array( 'plugin_files' => array( 'one/one.php' ), 'blocked' => false ) );
check( $released['plugins'][0]['verified'] && ! $released['plugins'][0]['configured'] && ! $released['plugins'][0]['blocked'], 'Releasing does not opt into auto updates' );
$fail_save = true;
check( ! Policy::write( $request )['plugins'][0]['verified'], 'Failed storage must not claim success' );
$fail_save = false;
add_filter( 'auto_update_plugin', function ( $value ) { return true; }, PHP_INT_MAX, 2 );
check( ! Policy::write( $request )['plugins'][0]['verified'], 'Conflicting later filter is not success' );
check( strpos( Policy::setting_html( 'Enable', 'one/one.php' ), 'Kosmos Hub' ) !== false, 'WordPress explains the lock' );
check( Policy::setting_html( 'Enable', 'other.php' ) === 'Enable', 'Unrelated UI unchanged' );
foreach ( array( Policy::READ_ABILITY, Policy::WRITE_ABILITY ) as $name ) {
	$definition = Registry::get_fallback_ability( $name );
	check( $definition['meta']['annotations']['readonly'] === ( Policy::READ_ABILITY === $name ), 'Correct read/write classification' );
}
Registry::register_abilities();
$native = $GLOBALS['abilities'][ Policy::WRITE_ABILITY ];
$admin = false;
check( false === call_user_func( $native['permission_callback'] ), 'Native API denies anonymous requests' );
$before = $options;
check( call_user_func( $native['execute_callback'], $request ) instanceof WP_Error && $before === $options, 'Native callback itself denies bypass' );
$admin = true;
check( true === call_user_func( $native['permission_callback'] ), 'Native API permits plugin administrators' );
check( is_array( call_user_func( $native['execute_callback'], $request ) ), 'Native write callback is callable' );
// Neither the policy nor its boot hooks enter any manual installation/update path.
check( ! isset( $filters['upgrader_pre_install'] ) && ! isset( $filters['pre_site_transient_update_plugins'] ), 'Manual updates and offers remain untouched' );
echo "Bridge auto-update policy contracts passed.\n";
