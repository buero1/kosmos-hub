<?php
// Offline harness. Use the real, hash-pinned WordPress Options API, including its caches/upsert.
define('ABSPATH', __DIR__);
define('HOUR_IN_SECONDS', 3600);
define('MINUTE_IN_SECONDS', 60);
$cache = array();
$counter = 0;
$checks = 0;
$domain = 'https://template.example/';
$requests = array();
$responses = array();
$scheduled = array();
set_error_handler(function($severity, $message, $file, $line) {
    throw new ErrorException($message, 0, $severity, $file, $line);
});
function apply_filters($name, $value, ...$args) {
    return $value;
}
function esc_sql($value) { return $value; }
function do_action(...$args) {}
function do_action_deprecated(...$args) {}
function wp_installing() { return false; }
function wp_doing_cron() { return false; }
function wp_doing_ajax() { return false; }
function wp_next_scheduled($hook) { global $scheduled; return $scheduled[$hook] ?? false; }
function wp_schedule_single_event($when,$hook) { global $scheduled; $scheduled[$hook]=$when; }
function wp_clear_scheduled_hook($hook) { global $scheduled; unset($scheduled[$hook]); }
function wp_using_ext_object_cache() { return true; }
function sanitize_option($name, $value) { return $value; }
function wp_cache_get($key, $group='', ...$args) { global $cache; return $cache[$group][$key] ?? false; }
function wp_cache_set($key, $value, $group='', ...$args) { global $cache; $cache[$group][$key]=$value; return true; }
function wp_cache_add($key, $value, $group='', ...$args) {
    global $cache;
    if (isset($cache[$group][$key])) return false;
    return wp_cache_set($key,$value,$group);
}
function wp_cache_delete($key, $group='') { global $cache; unset($cache[$group][$key]); return true; }
function maybe_serialize($value) { return is_array($value) || is_object($value) ? serialize($value) : $value; }
function maybe_unserialize($value) {
    return is_string($value) && preg_match('/^[aObis]:/', $value) ? unserialize($value) : $value;
}
function home_url($path='/') { global $domain; return $domain; }
function site_url($path='/') { return home_url($path); }
function rest_url($path) { return home_url().$path; }
function wp_parse_url($url,$component=-1) { return parse_url($url,$component); }
function wp_generate_uuid4() { global $counter; return sprintf('00000000-0000-4000-8000-%012d',++$counter); }
function wp_generate_password($length,...$args) { return str_repeat('x',$length); }
function get_bloginfo($key) { return '6.9'; }
function wp_json_encode($value) { return json_encode($value); }
function trailingslashit($value) { return rtrim($value,'/').'/'; }
function __($value,...$args) { return $value; }
class WP_Error {
    private $code; private $message;
    function __construct($code,$message,...$args) { $this->code=$code; $this->message=$message; }
    function get_error_code() { return $this->code; }
    function get_error_message() { return $this->message; }
}
function is_wp_error($value) { return $value instanceof WP_Error; }
function wp_remote_post($url,$args) {
    global $requests, $responses;
    $payload=json_decode($args['body'],true);
    $requests[]=array('url'=>$url,'args'=>$args,'payload'=>$payload);
    if (!$responses) throw new Exception('Unexpected HTTP request (network is disabled).');
    $response=array_shift($responses);
    return is_callable($response) ? $response($payload, $args) : $response;
}
function wp_remote_retrieve_response_code($value) { return $value['code']; }
function wp_remote_retrieve_body($value) { return $value['body']; }
function wp_remote_get($url,$args) {
    global $responses;
    if (!$responses) throw new Exception('Unexpected HTTP GET (network is disabled).');
    $response=array_shift($responses);
    return is_callable($response) ? $response($url,$args) : $response;
}
class BridgeFixtureDb {
    public $options='wp_options';
    public $rows=array();
    public $last_error='';
    public $fail_reads=false;
    public $fail_writes=false;
    public $before_query=null;
    function suppress_errors($value=true) { return false; }
    function get_results($query) {
        if (strpos($query,'SELECT option_name, option_value FROM wp_options')!==0) throw new Exception('Unsupported fixture options query');
        $rows=$this->rows;
        if (strpos($query,'WHERE autoload IN')!==false) {
            $rows=array_filter($rows,function($row) { return in_array($row['autoload'] ?? 'no',array('yes','on','auto-on','auto'),true); });
        }
        return array_map(function($row) { return (object)$row; },array_values($rows));
    }
    function prepare($query,...$args) { return array($query,$args); }
    function get_row($prepared) {
        $this->last_error=$this->fail_reads ? 'Simulated database read failure' : '';
        if ($this->fail_reads) return null;
        $name=$prepared[1][0];
        return isset($this->rows[$name]) ? (object)$this->rows[$name] : null;
    }
    function get_var($prepared) {
        if (strpos($prepared[0],'SELECT autoload ')!==0) throw new Exception('Unsupported fixture scalar query');
        return $this->rows[$prepared[1][0]]['autoload'] ?? null;
    }
    function query($prepared) {
        list($query,$args)=$prepared;
        if ($this->before_query) {
            $hook=$this->before_query;
            $this->before_query=null;
            $hook($query,$args);
        }
        $this->last_error=$this->fail_writes ? 'Simulated database write failure' : '';
        if ($this->fail_writes) return false;
        if (strpos($query,'INSERT IGNORE')===0) {
            list($name,$value)=$args;
            if (isset($this->rows[$name])) return 0;
            $this->rows[$name]=array('option_name'=>$name,'option_value'=>$value,'autoload'=>'no');
            return 1;
        }
        if (strpos($query,'UPDATE ')===0 && strpos($query,'AND BINARY option_value')!==false) {
            list($value,$name,$expected)=$args;
            if (!isset($this->rows[$name]) || $this->rows[$name]['option_value']!==$expected) return 0;
            $this->rows[$name]['option_value']=$value;
            $this->rows[$name]['autoload']='no';
            return 1;
        }
        if (strpos($query,'DELETE FROM')===0 && strpos($query,'AND BINARY option_value')!==false) {
            list($name,$expected)=$args;
            if (!isset($this->rows[$name]) || $this->rows[$name]['option_value']!==$expected) return 0;
            unset($this->rows[$name]);
            return 1;
        }
        if (strpos($query,'INSERT INTO')===0 && strpos($query,'ON DUPLICATE KEY UPDATE')!==false) {
            list($name,$value,$autoload)=$args;
            $changed=isset($this->rows[$name]) ? 2 : 1;
            $this->rows[$name]=array('option_name'=>$name,'option_value'=>$value,'autoload'=>$autoload);
            return $changed;
        }
        throw new Exception('Unsupported fixture SQL: '.$query);
    }
    function update($table,$values,$where) {
        $name=$where['option_name'];
        if (!isset($this->rows[$name])) return 0;
        $this->rows[$name]=array_merge($this->rows[$name],$values);
        return 1;
    }
    function delete($table,$where) {
        $name=$where['option_name'];
        $exists=isset($this->rows[$name]);
        unset($this->rows[$name]);
        return $exists ? 1 : 0;
    }
}
function check($condition,$message) {
    global $checks;
    ++$checks;
    if (!$condition) throw new Exception($message);
}
function reset_fixture($options=array()) {
    global $wpdb,$cache,$requests,$responses,$scheduled,$domain;
    $wpdb=new BridgeFixtureDb();
    $wpdb->rows['blogname']=array('option_name'=>'blogname','option_value'=>'Offline fixture','autoload'=>'yes');
    $cache=$requests=$responses=$scheduled=array();
    $domain='https://template.example/';
    foreach ($options as $key=>$value) {
        $wpdb->rows[$key]=array('option_name'=>$key,'option_value'=>maybe_serialize($value),'autoload'=>'no');
    }
}
$fixture=getenv('WP_OPTIONS_FIXTURE') ?: __DIR__.'/../../tmp/wordpress-6.9-option.php';
if (!is_file($fixture) || hash_file('sha256',$fixture)!=='cde15dc93f943de5884c87b26f0786011c6e43b93440cde1792428029ced9e37') {
    throw new Exception('Set WP_OPTIONS_FIXTURE to the hash-pinned WordPress 6.9 wp-includes/option.php; see docs/bridge-registration-safety.md.');
}
reset_fixture();
require $fixture;
foreach (array('Options','Registration/OptionStore','Registration/SecretStore','Registration/RegistrationState',
               'Registration/PayloadFactory','Http/RegistrationClient','Registration/Registrar','Plugin') as $class) {
    require __DIR__.'/../../wordpress-plugin/src/'.$class.'.php';
}
$success=function($payload) {
    return array('code'=>200,'body'=>json_encode(array('site_uuid'=>$payload['site_uuid'],'status'=>'verified')));
};
