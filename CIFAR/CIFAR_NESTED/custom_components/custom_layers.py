import tensorflow as tf
from tensorflow.keras.initializers import RandomNormal
import numpy as np
import logging

eps_float32 = np.finfo(np.float32).eps

log_dir = ""

def setup_logger(new_log_dir, logs):
    # Set up the first logger for total loss
    global log_dir
    # Update the global log_dir
    log_dir = new_log_dir

    if logs != []:
        for log in logs:
            # Set up the logger
            total_loss_logger = tf.get_logger()
            total_loss_handler = logging.FileHandler(f'{new_log_dir}/{log}', mode='a')
            total_loss_handler.setFormatter(logging.Formatter('%(message)s'))
            total_loss_logger.addHandler(total_loss_handler)
            total_loss_logger.setLevel(logging.INFO)

            # Clear the content
            with open(f'{new_log_dir}/{log}', 'w'):
                pass


class MinValueConstraint(tf.keras.constraints.Constraint):
    def __init__(self, min_value):
        self.min_value = min_value

    def __call__(self, w):
        return tf.maximum(w, self.min_value)

    def get_config(self):
        return {'min_value': self.min_value}
    

@tf.custom_gradient
def my_custom_gradient(parameter, scale, penalty_rate):
    penalty_rate = tf.stop_gradient(penalty_rate)
    inputs_quantized_nonrounded = parameter / scale # changes in the scale affect the output inversely    
    inputs_quantized_rounded = tf.stop_gradient(tf.floor(inputs_quantized_nonrounded))
    inputs_quantized_scaled_back = inputs_quantized_rounded * scale

    def custom_grad(dy, variables=None):
        if len(parameter.shape) == 1:
            maxvalue = tf.expand_dims(tf.reduce_max(tf.abs(tf.floor(parameter / scale)), axis=0), axis=0)
            scale_grads = tf.expand_dims(tf.reduce_mean(dy * inputs_quantized_rounded, axis=0), axis=0)
        else:
            maxvalue = tf.expand_dims(tf.reduce_max(tf.abs(tf.floor(parameter / scale)), axis=1), axis=1)
            scale_grads = tf.expand_dims(tf.reduce_mean(dy * inputs_quantized_rounded, axis=1), axis=1)

        penalty = tf.where(maxvalue <= 1.0, 0.0, penalty_rate * maxvalue)

        scale_grads *= penalty

        return dy, scale_grads, None

    return inputs_quantized_scaled_back, custom_grad


class CustomQuantizedScaleLayer(tf.keras.layers.Layer):

    def __init__(self, penalty_rate, initializer, orientation):
        super(CustomQuantizedScaleLayer, self).__init__()
        self.initializer = initializer
        self.orientation = orientation
        self.penalty_rate = penalty_rate

        setup_logger("logs/test", ["call_values.log", "scale_grads.log", "maxvalue.log", "signs.log"])

    def build(self, input_shape):
        shape = list(input_shape)
        
        # no None dimensions, so adjust
        if len(shape) == 1:
            shape = [1,1]
        elif input_shape[0] is None:
            shape[0] = 1
        elif input_shape[1] is None:
            shape[1] = 1

        if self.orientation == "rowwise":
            self.scale = self.add_weight(name="Rowwise-scaler", shape=(shape[0], 1), initializer=tf.keras.initializers.Constant(eps_float32*100), trainable=True, constraint = MinValueConstraint(eps_float32*100))
        elif self.orientation == "columnwise": # aroun 1k times lower than max
            self.scale = self.add_weight(name="Columnwise-scaler", shape=(1, shape[1]), initializer=tf.keras.initializers.Constant(eps_float32*100), trainable=True, constraint = MinValueConstraint(eps_float32*100))
        else:
            self.scale = self.add_weight(name="Scalar-scaler",shape=(1, ), initializer=tf.keras.initializers.Constant(eps_float32*100), trainable=True, constraint = MinValueConstraint(eps_float32*100))


    def call(self, inputs): 
        tf.print("Called", output_stream=f'file://logs/test/call_values.log')

        return my_custom_gradient(inputs, self.scale, self.penalty_rate)

    def get_scale(self):
        return self.scale
    

class CustomDenseLayer(tf.keras.layers.Layer):

    def __init__(self, units, penalty_rate, seed, orientation="rowwise", l2_factor=0.0001):
        super(CustomDenseLayer, self).__init__()
        self.units = units
        self.nested_q_w_layer = CustomQuantizedScaleLayer(penalty_rate=penalty_rate, initializer=None, orientation=orientation)
        self.nested_q_b_layer = CustomQuantizedScaleLayer(penalty_rate=penalty_rate, initializer=None, orientation="scalar")

        self.l2_factor = l2_factor

        setup_logger("logs/test", ["default_call_values.log"])
        
        self.seed = seed

    def build(self, input_shape):
        self.W = self.add_weight(
            name="Weights",
            shape=(input_shape[-1], self.units),
            initializer=tf.keras.initializers.RandomNormal(seed=self.seed),
            regularizer=tf.keras.regularizers.l2(0.001),
            trainable=True
        )

        self.b = self.add_weight(
            name="Bias",
            shape=(self.units,),
            initializer=tf.keras.initializers.RandomNormal(seed=self.seed),
            regularizer=tf.keras.regularizers.l2(0.00001),
            trainable=True
        )

    def call(self, inputs): 

        qw = self.nested_q_w_layer(self.W)
        qb = self.nested_q_b_layer(self.b)

        tf.print("Called", output_stream=f'file://logs/test/default_call_values.log')

        return tf.add(tf.matmul(inputs, qw), qb)


class CustomConv2DLayer(tf.keras.layers.Layer):
    def __init__(self, filters, kernel_size, strides=(1, 1), padding="same", activation=None, **kwargs):
        super(CustomConv2DLayer, self).__init__(**kwargs)
        self.filters = filters
        self.kernel_size = kernel_size
        self.strides = strides
        self.padding = padding.upper()  # 'valid' or 'same'
        self.activation = tf.keras.activations.get(activation)

    def build(self, input_shape):
        # Initialize kernel weights and bias
        kernel_shape = (*self.kernel_size, input_shape[-1], self.filters)
        self.kernel = self.add_weight(
            name="kernel",
            shape=kernel_shape,
            initializer="glorot_uniform",
            trainable=True,
        )
        self.bias = self.add_weight(
            name="bias",
            shape=(self.filters,),
            initializer="zeros",
            trainable=True,
        )

    def call(self, inputs):
        # Perform convolution
        conv_output = tf.nn.conv2d(
            inputs,
            filters=self.kernel,
            strides=self.strides,
            padding=self.padding,
        )

        return tf.add(conv_output, self.bias)